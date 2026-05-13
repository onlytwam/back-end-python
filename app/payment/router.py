"""
app/payment/router.py
─────────────────────
PayG payment endpoints wired into FastAPI.

  GET  /api/Payment/Callback          – browser redirect from PayG (success/fail)
  POST /api/Payment/Callback          – some PayG flows POST the result
  GET  /api/Payment/Status/{order_id} – frontend polls for payment status
  POST /api/Payment/Expire/{order_id} – frontend calls on 10-min timeout to mark order PaymentFailed
  GET  /api/Payment/Debug/{order_id}  – admin: instant full diagnosis
  GET  /api/Payment/Config            – admin: view current PayG settings (masked)
  PUT  /api/Payment/Config            – admin: update PayG settings in the DB
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from database import get_db
from app.auth.dependencies import get_current_user, require_roles, Roles, CurrentUser
from app.orders.models import Orders, OrderTrackingStatus, OrderRefund
from app.shared.app_settings import get_setting, reload_settings
from . import payg_client

router = APIRouter(prefix="/api/Payment", tags=["Payment"])


# ── Helpers ────────────────────────────────────────────────────────────────

def _frontend_url() -> str:
    return get_setting("frontend_base_url", "http://localhost:5173")


def _record_payment_log(
    db: Session,
    order_id: int,
    order_key_id: str,
    unique_request_id: str,
    payload: dict,
) -> None:
    try:
        merch_key_id_str = get_setting("payg_merchant_key_id", "0")
        merch_key_id = int(merch_key_id_str) if merch_key_id_str.isdigit() else 0
        log = OrderRefund(
            orderId                  = order_id,
            orderKeyId               = order_key_id,
            merchanKeyId             = merch_key_id,
            uniqueRequestId          = unique_request_id,
            orderStatus              = payload.get("OrderStatus", ""),
            paymentStatus            = payload.get("PaymentStatus"),
            paymentTransactionId     = payload.get("PaymentTransactionId", ""),
            paymentResponseCode      = payload.get("PaymentResponseCode"),
            paymentReasonCode        = payload.get("PaymentReasonCode", ""),
            paymentTransactionRefNo  = payload.get("PaymentTransactionRefNo", ""),
            paymentMethod            = payload.get("PaymentMethod", ""),
            paymentAccount           = payload.get("PaymentAccount", ""),
            createdDate              = datetime.now(timezone.utc),
            modifiedDate             = datetime.now(timezone.utc),
            deletedInd               = False,
        )
        db.add(log)
    except Exception as exc:
        print(f"[Payment] OrderRefund log failed for order {order_id}: {exc}")


def _update_order_after_payment(
    db: Session,
    order: Orders,
    response_code: int,
    payload: dict,
) -> None:
    new_state = payg_client.payment_code_to_order_state(response_code)
    order.state                   = new_state
    order.paymentMethod           = payload.get("PaymentMethod") or order.paymentMethod
    order.paymentAccount          = payload.get("PaymentAccount") or order.paymentAccount
    order.paymentTransactionId    = payload.get("PaymentTransactionId", "")
    order.paymentTransactionRefNo = payload.get("PaymentTransactionRefNo", "")
    order.orderKeyId              = payload.get("OrderKeyId") or order.orderKeyId
    order.modifiedDate            = datetime.now(timezone.utc)
    db.add(order)
    db.add(OrderTrackingStatus(
        orderId     = order.orderId,
        status      = new_state,
        createdDate = datetime.now(timezone.utc),
    ))


def _send_payment_notification(db: Session, order_id: int, success: bool) -> None:
    try:
        if success:
            from app.ekart.notifications import send_order_placed_notification
            send_order_placed_notification(db, order_id)
        else:
            from app.ekart.notifications import send_payment_failure_notification
            send_payment_failure_notification(db, order_id)
    except Exception as exc:
        print(f"[Payment] Notification error for order {order_id}: {exc}")


def _enrich_params_via_detail_api(oid: str, rid: str) -> dict:
    detail: dict = {}
    try:
        detail = payg_client.get_payg_order_detail(unique_request_id=rid)
        print(
            f"[Payment] Detail API → code={detail.get('PaymentResponseCode')} "
            f"status={detail.get('OrderStatus')} keyId={detail.get('OrderKeyId')}"
        )
    except Exception as exc:
        print(f"[Payment] ❌ Detail API FAILED for rid={rid}: {exc}")
        return {}

    txn_list = detail.get("OrderPaymentTransactionDetail") or []
    txn = txn_list[0] if txn_list else {}

    return {
        "UserDefined1":            oid,
        "OrderKeyId":              detail.get("OrderKeyId", ""),
        "UniqueRequestId":         detail.get("UniqueRequestId", rid),
        "PaymentResponseCode":     detail.get("PaymentResponseCode", -1),
        "PaymentStatus":           detail.get("PaymentStatus"),
        "OrderStatus":             detail.get("OrderStatus", ""),
        "PaymentMethod":           detail.get("PaymentMethod") or txn.get("PaymentMethod", ""),
        "PaymentAccount":          detail.get("PaymentAccount") or txn.get("PaymentAccount", ""),
        "PaymentTransactionId":    str(txn.get("TransactionId", "")),
        "PaymentTransactionRefNo": str(txn.get("TransactionId", "")),
        "PaymentReasonCode":       txn.get("ResponseText", ""),
        "MID":                     get_setting("payg_mid"),
        "OrderAmount":             str(detail.get("OrderAmount", "")),
    }


# ── AUTO EKART SHIPMENT ────────────────────────────────────────────────────

async def _auto_create_ekart_shipment(order_id: int) -> None:
    """
    Runs as a FastAPI background task after successful payment.
    Creates its OWN DB session — the request session is already closed.
    """
    from database import SessionLocal
    db = SessionLocal()
    try:
        from app.ekart.client import get_ekart_client_for_db, load_ekart_config_from_db
        from app.ekart.schemas import CreateShipmentRequest, EkartAddress, EkartShipmentItem
        from app.ekart.notifications import send_order_shipped_notification

        print(f"\n[Payment→Ekart] ▶▶▶ START order={order_id}")

        cfg = load_ekart_config_from_db(db)
        if cfg is None:
            print(f"[Payment→Ekart] ❌ No EKart config in DB — check ShipmentConfigurations table")
            return

        print(
            f"[Payment→Ekart] Config: client_id={cfg.client_id} | "
            f"alias={cfg.pickup_alias!r} | complete={cfg.is_complete}"
        )

        row = db.execute(text("""
            SELECT
                o."OrderId", o."OrderNumber", o."TotalAmount", o."State",
                o."TrackingId", o."PaymentMethod", o."DeliveryCharge",
                a."Name", a."Phone", a."AddressLine", a."City",
                a."PinCode", s."StateName", c."CountryName",
                p."EmailId",
                o."ShippingTypeId", st."ShippingTypeName"
            FROM twam."Orders" o
            LEFT JOIN twam."Address" a   ON a."AddressId"    = o."ShippingAddressId"
            LEFT JOIN mdm."State" s      ON s."StateId"      = a."StateId"
            LEFT JOIN mdm."Country" c    ON c."CountryId"    = a."CountryId"
            LEFT JOIN twam."People" p    ON p."UserProfileId"= o."UserProfileId"
            LEFT JOIN mdm."ShippingType" st ON st."ShippingTypeId" = o."ShippingTypeId"
                AND st."DeletedInd" = false
            WHERE o."OrderId" = :oid AND o."DeletedInd" = false
        """), {"oid": order_id}).fetchone()

        if not row:
            print(
                f"[Payment→Ekart] ❌ Order {order_id} NOT IN DB!\n"
                f"   → Frontend is calling a LOCAL backend, not this Azure instance.\n"
                f"   → Fix: add frontend/.env.local with VITE_API_BASE_URL pointing to Azure."
            )
            return

        order = {
            "order_id":           row[0],
            "order_number":       row[1] or str(row[0]),
            "total_amount":       float(row[2]) if row[2] else 0,
            "state":              row[3],
            "tracking_id":        row[4],
            "payment_mode":       "Prepaid",
            "delivery_charge":    float(row[6]) if row[6] else 0,
            "name":               row[7] or "Customer",
            "phone":              row[8] or "",
            "address_line":       row[9] or "",
            "city":               row[10] or "",
            "state_name":         row[12] or "",
            "pincode":            row[11] or "",
            "country":            row[13] or "India",
            "email":              row[14],
            "shipping_type_name": row[16] or "Normal",
        }

        print(
            f"[Payment→Ekart] Order: state={order['state']} tracking={order['tracking_id']!r} "
            f"city={order['city']} pin={order['pincode']} total=₹{order['total_amount']}"
        )

        if order["tracking_id"]:
            print(f"[Payment→Ekart] ⚠️  Already has tracking_id — skip")
            return

        if order["state"] in ("Shipped", "Delivered", "Cancelled"):
            print(f"[Payment→Ekart] ⚠️  State='{order['state']}' — skip")
            return

        item_rows = db.execute(text("""
            SELECT
                p."Name",
                COALESCE(pv."ProductCode", CAST(oi."ProductId" AS TEXT)) AS sku,
                oi."Quantity", oi."Price",
                thc."HSNCode"
            FROM twam."OrderItems" oi
            LEFT JOIN twam."Products" p         ON p."ProductId"         = oi."ProductId"
            LEFT JOIN twam."ProductVariants" pv ON pv."ProductVariantId" = oi."ProductVariantId"
            LEFT JOIN mdm."TaxHSNCode" thc      ON thc."TaxHSNCodeId"    = p."TaxHSNCodeId"
            WHERE oi."OrderId" = :oid AND oi."DeletedInd" = false
        """), {"oid": order_id}).fetchall()

        items = [
            EkartShipmentItem(
                name=r[0] or "Product",
                sku=r[1],
                quantity=r[2] or 1,
                price=float(r[3]) if r[3] else 0,
                hsn_code=r[4],
            )
            for r in item_rows
        ]
        print(f"[Payment→Ekart] {len(items)} item(s) fetched")

        # ── Surprise gift: if physical_gift type, add to shipment at ₹0 ─────
        try:
            from app.surprisegift.repository import get_order_gift_snapshot
            gift_snap = get_order_gift_snapshot(db, order_id)
            if gift_snap and gift_snap.giftType == "physical_gift":
                items.append(EkartShipmentItem(
                    name=gift_snap.giftName,
                    sku=gift_snap.sku or f"GIFT-{gift_snap.giftRuleId}",
                    quantity=1,
                    price=0.0,
                    hsn_code=gift_snap.hsnCode,
                ))
                print(f"[Payment→Ekart] 🎁 Added physical gift '{gift_snap.giftName}' (sku={gift_snap.sku}) to shipment")
            elif gift_snap and gift_snap.giftType == "physical_coupon":
                print(f"[Payment→Ekart] 🎁 Physical coupon gift — no ekart item needed (packed by inventory)")
        except Exception as _gexc:
            print(f"[Payment→Ekart] ⚠️  Could not fetch surprise gift for order {order_id}: {_gexc}")

        request = CreateShipmentRequest(
            order_id=            order["order_id"],
            order_number=        order["order_number"],
            consignee_name=      order["name"],
            consignee_phone=     order["phone"],
            consignee_address=EkartAddress(
                name=           order["name"],
                phone=          order["phone"],
                address_line1=  order["address_line"],
                city=           order["city"],
                state=          order["state_name"],
                pincode=        order["pincode"],
                country=        order["country"],
            ),
            payment_mode=        "COD" if order["payment_mode"] == "COD" else "Prepaid",
            cod_amount=          order["total_amount"] if order["payment_mode"] == "COD" else None,
            total_amount=        order["total_amount"],
            items=               items,
            shipping_type_name=  order.get("shipping_type_name", "Normal"),
        )

        client = get_ekart_client_for_db(db)
        if client.mock_mode:
            print(f"[Payment→Ekart] ⚠️  MOCK MODE — shipment NOT real (config incomplete?)")

        print(f"[Payment→Ekart] Calling Ekart API...")
        result = await client.create_shipment(request)

        if not result.success:
            print(
                f"[Payment→Ekart] ❌ FAILED: {result.error_message}\n"
                f"   → Admin retry: POST /api/Ekart/CreateShipment"
            )
            return

        print(f"[Payment→Ekart] ✅ AWB={result.awb_number} courier={result.courier_name}")

        db.execute(text("""
            UPDATE twam."Orders"
            SET "TrackingId"    = :tid,
                "State"         = 'Processing',
                "IsShipped"     = true,
                "DeliveryAgent" = 'EKart',
                "ModifiedDate"  = :now
            WHERE "OrderId" = :oid
        """), {"tid": result.awb_number, "oid": order_id, "now": datetime.now(timezone.utc)})

        db.add(OrderTrackingStatus(
            orderId=order_id,
            status="Processing",
            createdDate=datetime.now(timezone.utc),
        ))
        db.commit()
        print(f"[Payment→Ekart] ✅ Order updated → Processing, TrackingId={result.awb_number}\n")

        send_order_shipped_notification(db, order_id)

    except Exception as exc:
        import traceback
        print(f"[Payment→Ekart] ❌ UNEXPECTED ERROR order={order_id}: {exc}")
        traceback.print_exc()
    finally:
        db.close()


# ── Callback ───────────────────────────────────────────────────────────────

@router.get("/Callback",  include_in_schema=True)
@router.post("/Callback", include_in_schema=True)
async def payment_callback(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    # ── 1. Parse params ───────────────────────────────────────────────────
    params: dict = {}
    if request.method == "GET":
        params = dict(request.query_params)
    else:
        ct = request.headers.get("content-type", "")
        if "application/json" in ct:
            try:
                params = await request.json()
            except Exception:
                params = {}
        else:
            try:
                form = await request.form()
                params = dict(form)
            except Exception:
                params = {}

    print(f"\n{'='*60}")
    print(f"[Payment] ▶ Callback {request.method} | URL={request.url}")
    print(f"[Payment]   params={json.dumps(params, default=str)}")

    oid = params.get("oid", "")
    rid = params.get("rid", "")

    payg_gave_us_params = bool(
        params.get("PaymentResponseCode") not in (None, "", "-1", -1)
        or params.get("OrderKeyId")
        or params.get("UserDefined1")
    )

    print(f"[Payment] oid={oid!r} rid={rid!r} payg_gave_us_params={payg_gave_us_params}")

    # ── 2. Enrich from Detail API if PayG gave nothing ────────────────────
    if not payg_gave_us_params and (oid or rid):
        print(f"[Payment] Calling PayG Detail API...")
        enriched = _enrich_params_via_detail_api(oid, rid)
        if enriched:
            params = enriched
        else:
            print(f"[Payment] ⚠️  Detail API empty — using oid={oid!r}, code defaults to -1")

    # ── 3. Find order ─────────────────────────────────────────────────────
    order_id_str  = params.get("UserDefined1") or oid or ""
    order_key_id  = params.get("OrderKeyId", "")
    unique_req_id = params.get("UniqueRequestId", "") or rid

    order: Optional[Orders] = None
    if order_id_str:
        try:
            order = db.query(Orders).filter(
                Orders.orderId    == int(order_id_str),
                Orders.deletedInd == False,
            ).first()
        except Exception as e:
            print(f"[Payment] id lookup error: {e}")

    if order is None and order_key_id:
        order = db.query(Orders).filter(
            Orders.orderKeyId  == order_key_id,
            Orders.deletedInd  == False,
        ).first()

    if order is None and unique_req_id:
        order = db.query(Orders).filter(
            Orders.paymentTransactionRefNo == unique_req_id,
            Orders.deletedInd              == False,
        ).first()

    if order is None:
        print(
            f"[Payment] ❌ ORDER NOT FOUND in Azure DB!\n"
            f"   order_id_str={order_id_str!r}\n"
            f"   ROOT CAUSE: frontend called local backend (localhost:8000), not Azure.\n"
            f"   FIX: create frontend/.env.local → VITE_API_BASE_URL=<azure_url>\n"
            f"        and add http://localhost:5173 to Azure CORS_ORIGINS env var."
        )
        return RedirectResponse(
            url=f"{_frontend_url()}/user-products/payment-callback?status=error&message=Order+not+found",
            status_code=302,
        )

    order_id = order.orderId
    print(f"[Payment] ✅ Order found: id={order_id} number={order.orderNumber} state={order.state}")

    # ── 4. Hash (skipped in UAT) ──────────────────────────────────────────
    is_uat  = get_setting("payg_mode", "uat").lower() == "uat"
    hash_ok = payg_client.verify_callback_hash(params)
    if not is_uat and not hash_ok:
        print(f"[Payment] ❌ Hash mismatch for order {order_id}")
        return RedirectResponse(
            url=f"{_frontend_url()}/user-products/payment-callback?status=error&orderId={order_id}&message=Invalid+signature",
            status_code=302,
        )

    # ── 5. Outcome ────────────────────────────────────────────────────────
    try:
        response_code = int(params.get("PaymentResponseCode", -1))
    except (ValueError, TypeError):
        response_code = -1

    success = (response_code == 1)
    print(f"[Payment] code={response_code} success={success} uat={is_uat}")

    try:
        _update_order_after_payment(db, order, response_code, params)
        _record_payment_log(db, order_id, order_key_id, unique_req_id, params)
        db.commit()
        print(f"[Payment] ✅ DB updated → {payg_client.payment_code_to_order_state(response_code)}")
    except Exception as exc:
        db.rollback()
        print(f"[Payment] ❌ DB update failed: {exc}")

    # ── 6. On success: queue Ekart shipment + mark cart items ordered ─────
    # Cart items are marked IsOrdered=true HERE (not at order-creation time).
    # This means if payment fails the items remain visible in the cart so
    # the user can review their order and retry without losing anything.
    if success:
        print(f"[Payment] ✅ SUCCESS — queueing Ekart task for order {order_id}")
        background_tasks.add_task(_auto_create_ekart_shipment, order_id)

        # Mark the cart rows that match this order's items as ordered.
        # Uses a JOIN on ProductVariantDetailId + UserProfileId so we only
        # touch the exact items that were just paid for, not other cart rows.
        try:
            db.execute(text("""
                UPDATE twam."Cart" c
                SET "IsOrdered" = true
                FROM twam."OrderItems" oi
                WHERE oi."OrderId" = :oid
                  AND oi."DeletedInd" = false
                  AND c."ProductVariantDetailId" = oi."ProductVariantDetailId"
                  AND c."UserProfileId" = (
                      SELECT "UserProfileId" FROM twam."Orders"
                      WHERE "OrderId" = :oid AND "DeletedInd" = false
                  )
                  AND c."DeletedInd" = false
                  AND (c."IsOrdered" IS NULL OR c."IsOrdered" = false)
            """), {"oid": order_id})
            db.commit()
            print(f"[Payment] ✅ Cart items marked as ordered for order {order_id}")
        except Exception as exc:
            db.rollback()
            print(f"[Payment] ⚠️  Failed to mark cart items ordered for order {order_id}: {exc}")
    else:
        print(f"[Payment] ⚠️  code={response_code} — Ekart NOT queued, cart preserved")

    # ── 7. Notify ─────────────────────────────────────────────────────────
    _send_payment_notification(db, order_id, success)

    # ── 8. Redirect ───────────────────────────────────────────────────────
    if success:
        redirect_url = f"{_frontend_url()}/user-products/order-confirm/{order_id}?payment=success"
    else:
        reason = params.get("PaymentReasonCode", "declined")
        redirect_url = (
            f"{_frontend_url()}/user-products/payment-callback"
            f"?status=failed&orderId={order_id}&code={response_code}&reason={reason}"
        )

    print(f"[Payment] Redirecting → {redirect_url}\n{'='*60}\n")
    return RedirectResponse(url=redirect_url, status_code=302)


# ── Debug endpoint (admin only) ────────────────────────────────────────────

@router.get("/Debug/{order_id}")
def debug_payment(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Admin diagnostic: shows whether the order exists in Azure DB, live PayG
    status, and full Ekart config state. Call this after any test payment to
    instantly see what went wrong without digging through Azure logs.
    """
    if current_user.role_id not in Roles.STAFF:
        raise HTTPException(403, "Admin only")

    order = db.query(Orders).filter(
        Orders.orderId    == order_id,
        Orders.deletedInd == False,
    ).first()

    if not order:
        return {
            "order_found": False,
            "diagnosis": (
                f"Order {order_id} does NOT exist in this Azure backend's DB. "
                "This means the frontend created the order by calling a LOCAL backend "
                "(localhost:8000) instead of this Azure instance. "
                "Fix: create frontend/.env.local with VITE_API_BASE_URL pointing "
                "to this Azure URL, then add http://localhost:5173 to Azure CORS_ORIGINS."
            ),
        }

    # Live PayG status
    payg_detail: dict = {}
    payg_error = ""
    if order.paymentTransactionRefNo or order.orderKeyId:
        try:
            payg_detail = payg_client.get_payg_order_detail(
                order_key_id      = order.orderKeyId or None,
                unique_request_id = order.paymentTransactionRefNo or None,
            )
        except Exception as e:
            payg_error = str(e)

    # Ekart config
    from app.ekart.client import load_ekart_config_from_db
    cfg = load_ekart_config_from_db(db)

    tracking_val = getattr(order, "trackingId", None) or getattr(order, "TrackingId", None)

    return {
        "order_found": True,
        "order": {
            "id":             order.orderId,
            "number":         order.orderNumber,
            "state":          order.state,
            "tracking_id":    tracking_val,
            "payment_method": order.paymentMethod,
            "ref_no":         order.paymentTransactionRefNo,
            "order_key_id":   order.orderKeyId,
        },
        "payg_live": {
            "response_code":  payg_detail.get("PaymentResponseCode"),
            "order_status":   payg_detail.get("OrderStatus"),
            "payment_status": payg_detail.get("PaymentStatus"),
            "amount":         payg_detail.get("OrderAmount"),
            "error":          payg_error or None,
        },
        "ekart": {
            "config_found":    cfg is not None,
            "config_complete": cfg.is_complete if cfg else False,
            "pickup_alias":    cfg.pickup_alias if cfg else None,
            "client_id":       cfg.client_id if cfg else None,
        },
        "settings": {
            "payg_mode":         get_setting("payg_mode"),
            "payg_redirect_url": get_setting("payg_redirect_url"),
            "frontend_base_url": get_setting("frontend_base_url"),
        },
    }


# ── Status poll ────────────────────────────────────────────────────────────

@router.get("/Status/{order_id}")
def get_payment_status(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    order = db.query(Orders).filter(
        Orders.orderId    == order_id,
        Orders.deletedInd == False,
    ).first()
    if not order:
        raise HTTPException(404, "Order not found")

    if current_user.role_id not in Roles.STAFF and order.userProfileId != current_user.user_id:
        raise HTTPException(403, "Not authorised")

    local_state = order.state or "Pending"
    payg_detail: dict = {}

    if order.orderKeyId or order.paymentTransactionRefNo:
        try:
            payg_detail = payg_client.get_payg_order_detail(
                order_key_id      = order.orderKeyId,
                unique_request_id = order.paymentTransactionRefNo,
            )
            live_code = payg_detail.get("PaymentResponseCode")
            if live_code is not None:
                live_state = payg_client.payment_code_to_order_state(int(live_code))
                if order.state != live_state:
                    order.state        = live_state
                    order.modifiedDate = datetime.now(timezone.utc)
                    db.add(order)
                    db.commit()
                    local_state = live_state
        except Exception as exc:
            print(f"[Payment] Live status check failed: {exc}")

    return {
        "orderId":              order_id,
        "orderNumber":          order.orderNumber,
        "state":                local_state,
        "paymentMethod":        order.paymentMethod,
        "paymentTransactionId": order.paymentTransactionId,
        "paymentResponseCode":  payg_detail.get("PaymentResponseCode"),
        "paygStatus":           payg_detail.get("OrderStatus"),
    }


# ── Payment expiry (frontend timeout) ─────────────────────────────────────

@router.post("/Expire/{order_id}")
def expire_payment(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Called by the frontend when the 10-minute payment window expires without
    a confirmed payment.  Marks the order as 'PaymentFailed' so it is hidden
    from the user's order list.  Only the order's owner (or staff) may call this.

    Safe to call even if the payment already succeeded — the guard below
    prevents overwriting a Confirmed/Processing/Delivered state.
    """
    order = db.query(Orders).filter(
        Orders.orderId    == order_id,
        Orders.deletedInd == False,
    ).first()

    if not order:
        raise HTTPException(404, "Order not found")

    if current_user.role_id not in Roles.STAFF and order.userProfileId != current_user.user_id:
        raise HTTPException(403, "Not authorised")

    # Only expire orders that are still waiting for payment — do NOT
    # overwrite a state that PayG has already resolved.
    SAFE_TO_EXPIRE = {"AwaitingPayment", "Pending", "Payment Pending"}
    if order.state not in SAFE_TO_EXPIRE:
        print(
            f"[Payment/Expire] Order {order_id} is in state '{order.state}' "
            f"— not expiring (payment may have succeeded)."
        )
        return {"success": False, "state": order.state, "message": "Order already resolved"}

    order.state        = "PaymentFailed"
    order.modifiedDate = datetime.now(timezone.utc)
    db.add(order)
    db.add(OrderTrackingStatus(
        orderId     = order_id,
        status      = "PaymentFailed",
        createdDate = datetime.now(timezone.utc),
    ))
    db.commit()
    print(f"[Payment/Expire] Order {order_id} expired → PaymentFailed")
    return {"success": True, "state": "PaymentFailed"}


# ── Admin: view / update PayG config ──────────────────────────────────────

class PaygConfigResponse(BaseModel):
    payg_mode:            str
    payg_mid:             str
    payg_auth_key:        str
    payg_auth_token:      str
    payg_secure_hash:     str
    payg_merchant_key_id: str
    payg_redirect_url:    str
    frontend_base_url:    str


class PaygConfigUpdate(BaseModel):
    payg_mode:            Optional[str] = None
    payg_mid:             Optional[str] = None
    payg_auth_key:        Optional[str] = None
    payg_auth_token:      Optional[str] = None
    payg_secure_hash:     Optional[str] = None
    payg_merchant_key_id: Optional[str] = None
    payg_redirect_url:    Optional[str] = None
    frontend_base_url:    Optional[str] = None


def _mask(v: str) -> str:
    if not v or len(v) <= 8:
        return "****"
    return v[:4] + "****" + v[-4:]


@router.get(
    "/Config",
    response_model = PaygConfigResponse,
    dependencies   = [Depends(require_roles(Roles.SUPER_ADMIN))],
)
def get_payg_config():
    return PaygConfigResponse(
        payg_mode            = get_setting("payg_mode", "uat"),
        payg_mid             = get_setting("payg_mid", ""),
        payg_auth_key        = _mask(get_setting("payg_auth_key", "")),
        payg_auth_token      = _mask(get_setting("payg_auth_token", "")),
        payg_secure_hash     = _mask(get_setting("payg_secure_hash", "")),
        payg_merchant_key_id = get_setting("payg_merchant_key_id", ""),
        payg_redirect_url    = get_setting("payg_redirect_url", ""),
        frontend_base_url    = get_setting("frontend_base_url", "http://localhost:5173"),
    )


@router.put(
    "/Config",
    dependencies = [Depends(require_roles(Roles.SUPER_ADMIN))],
)
def update_payg_config(
    data: PaygConfigUpdate,
    db: Session = Depends(get_db),
):
    from app.common.models import AppSettings

    updates = {k: v for k, v in data.model_dump().items() if v is not None}
    now = datetime.now(timezone.utc)

    for key, value in updates.items():
        row = (
            db.query(AppSettings)
            .filter(AppSettings.key == key, AppSettings.deletedInd == False)
            .first()
        )
        if row:
            row.value        = value
            row.modifiedDate = now
        else:
            db.add(AppSettings(
                key          = key,
                value        = value,
                description  = f"PayG setting: {key}",
                isActive     = True,
                createdDate  = now,
                modifiedDate = now,
                deletedInd   = False,
            ))

    db.commit()
    reload_settings(db)
    return {"success": True, "updated": list(updates.keys())}