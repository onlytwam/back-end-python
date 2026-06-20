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
import os
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


# ── Optional-auth dependency ───────────────────────────────────────────────
# get_current_user raises HTTPException(401/403) when no token is present.
# Simply typing a parameter as Optional[CurrentUser] does NOT suppress that
# raise — FastAPI still propagates it.  This wrapper catches any auth
# exception and returns None instead, which is what we need for the Status
# endpoint so unauthenticated polls (session lost after PayG redirect) still
# work.

async def get_current_user_optional(
    request: Request,
    db: Session = Depends(get_db),
) -> Optional[CurrentUser]:
    try:
        return await get_current_user(request, db)
    except Exception:
        return None


# ── Helpers ────────────────────────────────────────────────────────────────

def _frontend_url() -> str:
    return get_setting("frontend_base_url", "NOT_FOUND") or "NOT_FOUND"  #  PayG and causes silent failures


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


def _enrich_params_via_detail_api(oid: str, rid: str, order_key_id: str = "") -> dict:
    detail: dict = {}
    try:
        # Prefer OrderKeyId (most reliable) → fall back to UniqueRequestId (rid).
        # PayG Detail API doc: body = { "OrderKeyId": "...", "MID": "..." }
        # UniqueRequestId also works but OrderKeyId is the authoritative reference.
        if order_key_id:
            detail = payg_client.get_payg_order_detail(order_key_id=order_key_id)
        elif rid:
            detail = payg_client.get_payg_order_detail(unique_request_id=rid)
        else:
            print(f"[Payment] ❌ Detail API skipped — no rid or order_key_id available")
            return {}
        print(
            f"[Payment] Detail API → code={detail.get('PaymentResponseCode')} "
            f"status={detail.get('OrderStatus')} keyId={detail.get('OrderKeyId')}"
        )
    except Exception as exc:
        lookup_ref = rid or order_key_id
        print(f"[Payment] ❌ Detail API FAILED for ref={lookup_ref}: {exc}")
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
    Every step is logged so you can pinpoint exactly where it fails.
    """
    from database import SessionLocal
    db = SessionLocal()
    SEP = "─" * 60
    try:
        print(f"\n{SEP}")
        print(f"[Payment→Ekart] ▶▶▶ START auto-shipment for order={order_id}")
        print(f"[SEP]")

        # ── STEP 1: Load Ekart config ────────────────────────────────────────
        print(f"[Payment→Ekart] STEP 1 — Loading Ekart config from ShipmentConfigurations table...")
        from app.ekart.client import get_ekart_client_for_db, load_ekart_config_from_db
        from app.ekart.schemas import CreateShipmentRequest, EkartAddress, EkartShipmentItem
        from app.ekart.notifications import send_order_shipped_notification

        cfg = load_ekart_config_from_db(db)
        if cfg is None:
            print(
                f"[Payment→Ekart] STEP 1 ❌ FAILED — No EKart config found in DB.\n"
                f"   → FIX: Insert a row in shipment.ShipmentConfigurations with\n"
                f"     deliveryAgent ILIKE 'EKart%', deletedInd=false,\n"
                f"     stateId=NULL or 'Active' (case-insensitive).\n"
                f"   → Run GET /api/Ekart/Readiness to see full config status."
            )
            return

        print(
            f"[Payment→Ekart] STEP 1 ✅ Config loaded | "
            f"client_id={cfg.client_id!r} | alias={cfg.pickup_alias!r} | "
            f"complete={cfg.is_complete} | auth_url={cfg.access_token_url!r}"
        )
        if not cfg.is_complete:
            print(
                f"[Payment→Ekart] STEP 1 ⚠️  Config is INCOMPLETE — missing one of:\n"
                f"   accessTokenURL, createShipmentURL, userName, or password.\n"
                f"   Client will run in MOCK mode — no real AWB will be generated."
            )

        # ── STEP 2: Fetch order from DB ──────────────────────────────────────
        print(f"[Payment→Ekart] STEP 2 — Fetching order {order_id} from DB...")
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
                f"[Payment→Ekart] STEP 2 ❌ FAILED — Order {order_id} NOT IN DB!\n"
                f"   → This usually means the frontend called a LOCAL backend (localhost:8000)\n"
                f"     to place the order, but PayG is calling back to Azure.\n"
                f"   → FIX: ensure VITE_API_BASE_URL in frontend points to this Azure URL.\n"
                f"   → Also add http://localhost:5173 to Azure CORS_ORIGINS env var."
            )
            return

        order = {
            "order_id":           row[0],
            "order_number":       row[1] or str(row[0]),
            "total_amount":       float(row[2]) if row[2] else 0,
            "state":              row[3],
            "tracking_id":        row[4],
            "payment_mode":       row[5] if row[5] else "Prepaid",
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
            f"[Payment→Ekart] STEP 2 ✅ Order found | "
            f"state={order['state']!r} | tracking={order['tracking_id']!r} | "
            f"total=₹{order['total_amount']} | payment_mode={order['payment_mode']!r} | "
            f"name={order['name']!r} | phone={order['phone']!r} | "
            f"city={order['city']!r} | pin={order['pincode']!r} | "
            f"state_name={order['state_name']!r} | address={order['address_line']!r}"
        )

        # Warn on missing address fields — Ekart will reject if these are blank
        missing_addr = []
        if not order["name"]:         missing_addr.append("consignee name")
        if not order["phone"]:        missing_addr.append("phone")
        if not order["address_line"]: missing_addr.append("address_line (ShippingAddressId may be NULL)")
        if not order["city"]:         missing_addr.append("city")
        if not order["pincode"]:      missing_addr.append("pincode")
        if not order["state_name"]:   missing_addr.append("state_name (StateId not set on Address, or State not in mdm.State)")
        if missing_addr:
            print(f"[Payment→Ekart] STEP 2 ⚠️  MISSING address fields: {', '.join(missing_addr)}")

        # ── STEP 3: Guard checks ─────────────────────────────────────────────
        print(f"[Payment→Ekart] STEP 3 — Guard checks...")
        if order["tracking_id"]:
            print(f"[Payment→Ekart] STEP 3 ⚠️  Already has tracking_id={order['tracking_id']!r} — skipping (duplicate callback?)")
            return

        if order["state"] in ("Shipped", "Delivered", "Cancelled"):
            print(f"[Payment→Ekart] STEP 3 ⚠️  Order state is '{order['state']}' — skipping shipment creation")
            return

        print(f"[Payment→Ekart] STEP 3 ✅ Guards passed — proceeding to create shipment")

        # ── STEP 4: Fetch order items ────────────────────────────────────────
        print(f"[Payment→Ekart] STEP 4 — Fetching order items...")
        item_rows = db.execute(text("""
            SELECT
                p."Name",
                COALESCE(p."ProductCode", CAST(oi."ProductId" AS TEXT)) AS sku,
                oi."Quantity", oi."Price",
                thc."HSNCode"
            FROM twam."OrderItems" oi
            LEFT JOIN twam."Products" p         ON p."ProductId"         = oi."ProductId"
            LEFT JOIN twam."ProductVariants" pv ON pv."ProductVariantId" = oi."ProductVariantId"
            LEFT JOIN mdm."TaxHSNCode" thc      ON thc."TaxHSNCodeId"    = p."TaxHSNCodeId"
            WHERE oi."OrderId" = :oid AND oi."DeletedInd" = false
        """), {"oid": order_id}).fetchall()

        if not item_rows:
            print(
                f"[Payment→Ekart] STEP 4 ❌ FAILED — No OrderItems found for order {order_id}!\n"
                f"   → OrderItems may not have been committed before this background task ran.\n"
                f"   → Check twam.OrderItems for OrderId={order_id} and DeletedInd=false."
            )
            return

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
        print(
            f"[Payment→Ekart] STEP 4 ✅ {len(items)} item(s): "
            + ", ".join(f"{i.name}(qty={i.quantity}, ₹{i.price}, sku={i.sku})" for i in items)
        )

        # ── STEP 5: Surprise gift ────────────────────────────────────────────
        print(f"[Payment→Ekart] STEP 5 — Checking for surprise gift...")
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
                print(f"[Payment→Ekart] STEP 5 ✅ Physical gift added: '{gift_snap.giftName}' sku={gift_snap.sku!r}")
            elif gift_snap:
                print(f"[Payment→Ekart] STEP 5 ℹ️  Gift type={gift_snap.giftType!r} — no extra Ekart item needed")
            else:
                print(f"[Payment→Ekart] STEP 5 ℹ️  No surprise gift for this order")
        except Exception as _gexc:
            print(f"[Payment→Ekart] STEP 5 ⚠️  Could not fetch surprise gift (non-fatal): {_gexc}")

        # ── STEP 6: Build Ekart request ──────────────────────────────────────
        print(f"[Payment→Ekart] STEP 6 — Building Ekart shipment request...")
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
        print(
            f"[Payment→Ekart] STEP 6 ✅ Request built | "
            f"payment_mode={request.payment_mode} | total=₹{request.total_amount} | "
            f"items={len(items)} | shipping_type={request.shipping_type_name!r}"
        )

        # ── STEP 7: Check mock mode ──────────────────────────────────────────
        print(f"[Payment→Ekart] STEP 7 — Initialising Ekart client...")
        client = get_ekart_client_for_db(db)
        print(f"[Payment→Ekart] STEP 7 — Client mode: {'MOCK ⚠️' if client.mock_mode else 'LIVE ✅'}")

        if client.mock_mode:
            print(
                f"[Payment→Ekart] STEP 7 ❌ MOCK MODE — config missing/incomplete.\n"
                f"   → Order {order_id} stays in 'Confirmed' state. No AWB written.\n"
                f"   → FIX: ensure ShipmentConfigurations has clientId, userName, password,\n"
                f"     accessTokenURL, createShipmentURL, stateId=NULL or 'Active'.\n"
                f"   → Retry manually: POST /api/Ekart/CreateShipment {{\"order_id\": {order_id}}}"
            )
            return

        # ── STEP 8: Call Ekart API ───────────────────────────────────────────
        print(f"[Payment→Ekart] STEP 8 — Calling Ekart API (PUT /api/v1/package/create)...")
        result = await client.create_shipment(request)

        if not result.success:
            print(
                f"[Payment→Ekart] STEP 8 ❌ FAILED — Ekart API rejected the request.\n"
                f"   Error: {result.error_message}\n"
                f"   → Order {order_id} stays in 'Confirmed' — no TrackingId written.\n"
                f"   → Common causes: SWIFT_RESOURCE_NOT_FOUND (pickup alias not registered),\n"
                f"     invalid pincode, phone must be 10 digits, token expired.\n"
                f"   → Retry manually: POST /api/Ekart/CreateShipment {{\"order_id\": {order_id}}}"
            )
            # Annotate order notes so admin can see the failure reason in the DB
            try:
                db.execute(text("""
                    UPDATE twam."Orders"
                    SET "Notes" = CONCAT(COALESCE("Notes", ''), :note),
                        "ModifiedDate" = :now
                    WHERE "OrderId" = :oid
                """), {
                    "note": f"[Ekart auto-shipment FAILED: {(result.error_message or '')[:200]}] ",
                    "oid": order_id,
                    "now": datetime.now(timezone.utc),
                })
                db.commit()
                print(f"[Payment→Ekart] STEP 8 ℹ️  Failure reason written to Orders.Notes for order {order_id}")
            except Exception as _ne:
                db.rollback()
                print(f"[Payment→Ekart] STEP 8 ⚠️  Could not write Notes: {_ne}")
            return

        print(f"[Payment→Ekart] STEP 8 ✅ Ekart API SUCCESS | AWB={result.awb_number!r} | courier={result.courier_name!r}")

        # ── STEP 9: Update DB ────────────────────────────────────────────────
        print(f"[Payment→Ekart] STEP 9 — Writing AWB={result.awb_number!r} to DB for order {order_id}...")
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
        print(
            f"[Payment→Ekart] STEP 9 ✅ DB updated — order {order_id} → "
            f"State=Processing | TrackingId={result.awb_number!r}"
        )

        # ── STEP 9b: Increment ProcessedQuantity for each order item ─────────
        # Stock formula: available = stockQuantity - processedQuantity + returnedQuantity
        # We increment ProcessedQuantity here; AvailableQuantity is recomputed in-Python.
        print(f"[Payment→Ekart] STEP 9b — Updating ProcessedQuantity for order {order_id}...")
        try:
            from app.productvariantdetail.repository import increment_processed_quantity
            _now = datetime.now(timezone.utc)
            # Fetch (pvd_id, sum_qty) for this order
            pvd_rows = db.execute(text("""
                SELECT "ProductVariantDetailId", SUM("Quantity")::INTEGER AS ordered_qty
                FROM   twam."OrderItems"
                WHERE  "OrderId"               = :oid
                  AND  "DeletedInd"             = false
                  AND  "ProductVariantDetailId" IS NOT NULL
                GROUP BY "ProductVariantDetailId"
            """), {"oid": order_id}).fetchall()

            updated_count = 0
            for pvd_id_row, qty in pvd_rows:
                if increment_processed_quantity(db, pvd_id_row, qty, now=_now):
                    updated_count += 1

            db.commit()
            print(
                f"[Payment→Ekart] STEP 9b ✅ ProcessedQuantity updated for order {order_id} — "
                f"{updated_count} variant(s) updated"
            )
        except Exception as _se:
            db.rollback()
            print(
                f"[Payment→Ekart] STEP 9b ⚠️  Stock update FAILED (non-fatal) "
                f"for order {order_id}: {_se}"
            )

        # ── STEP 10: Notification ────────────────────────────────────────────
        print(f"[Payment→Ekart] STEP 10 — Sending shipped notification...")
        try:
            send_order_shipped_notification(db, order_id)
            print(f"[Payment→Ekart] STEP 10 ✅ Shipped notification sent for order {order_id}")
        except Exception as _nex:
            print(f"[Payment→Ekart] STEP 10 ⚠️  Notification failed (non-fatal): {_nex}")

        print(f"[Payment→Ekart] ✅✅✅ COMPLETE — order {order_id} shipment created. AWB={result.awb_number!r}")
        print(f"{SEP}\n")

    except Exception as exc:
        import traceback
        print(f"[Payment→Ekart] ❌ UNEXPECTED ERROR for order {order_id}: {exc}")
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
    SEP = "=" * 60
    # ── STEP 1: Parse incoming params from PayG ───────────────────────────
    # PayG sends the redirect as a browser GET with query params appended to
    # the RedirectUrl.  In some configurations it POSTs form data instead.
    # We merge BOTH query-string params AND body params so we never miss a field.
    params: dict = {}
    print(f"[Payment:Callback] frontend_base_url raw = {get_setting('frontend_base_url', 'NOT_FOUND')}")
    print(f"[Payment:Callback] _frontend_url() = {_frontend_url()}")
    print(f"Helloooooooooooooooooooooooooooooooooooooo")
    # Always read query-string params (present on both GET and POST)
    params.update(dict(request.query_params))

    if request.method == "POST":
        ct = request.headers.get("content-type", "")
        if "application/json" in ct:
            try:
                body_params = await request.json()
                if isinstance(body_params, dict):
                    params.update(body_params)
            except Exception:
                pass
        else:
            try:
                form = await request.form()
                params.update(dict(form))
            except Exception:
                pass

    print(f"\n{SEP}")
    print(f"[Payment:Callback] ▶ STEP 1 — Received {request.method} callback from PayG")
    print(f"[Payment:Callback]   URL={request.url}")
    print(f"[Payment:Callback]   Raw params={json.dumps(params, default=str)}")

    oid = params.get("oid", "")
    rid = params.get("rid", "")

    # PayG appends payment result fields to the RedirectUrl as query params:
    #   PaymentResponseCode, OrderKeyId, UniqueRequestId, UserDefined1, HashData, …
    # We treat params as "complete" when PaymentResponseCode is a real resolved
    # value (1=approved, 2=declined).  Codes 0/4 (pending) and absent/-1 mean
    # the payment hasn't settled yet — we still need to call the Detail API.
    payg_response_code_raw = params.get("PaymentResponseCode")
    try:
        payg_response_code_direct = int(payg_response_code_raw) if payg_response_code_raw not in (None, "", "-1") else None
    except (ValueError, TypeError):
        payg_response_code_direct = None

    payg_gave_us_params = payg_response_code_direct in (1, 2)

    print(
        f"[Payment:Callback] STEP 1 ✅ Parsed | oid={oid!r} | rid={rid!r} | "
        f"payg_gave_us_params={payg_gave_us_params} | "
        f"PaymentResponseCode={payg_response_code_raw!r} | "
        f"OrderKeyId={params.get('OrderKeyId')!r} | "
        f"UserDefined1={params.get('UserDefined1')!r}"
    )

    # ── STEP 2: Enrich from Detail API if PayG didn't give a resolved code ──
    # If PayG gave us code=1 or code=2 directly in the redirect params → skip.
    # If Detail API is IP-blocked (403) → log clearly and proceed with code=-1
    #   (frontend will poll via /Payment/Status and resolve the outcome).
    #
    # PayG only sends oid+rid in the redirect URL — no OrderKeyId param.
    # So we look up the order from DB using oid first, then pass the stored
    # orderKeyId to the Detail API (more reliable than UniqueRequestId).
    order_key_id_from_cb = params.get("OrderKeyId", "")
    if not order_key_id_from_cb and oid:
        try:
            _early_order = db.query(Orders).filter(
                Orders.orderId == int(oid),
                Orders.deletedInd == False,
            ).first()
            if _early_order and _early_order.orderKeyId:
                order_key_id_from_cb = _early_order.orderKeyId
                print(f"[Payment:Callback] STEP 2 — Resolved OrderKeyId={order_key_id_from_cb!r} from DB for oid={oid!r}")
        except Exception as _e:
            print(f"[Payment:Callback] STEP 2 — Could not pre-fetch orderKeyId: {_e}")

    if not payg_gave_us_params and (oid or rid or order_key_id_from_cb):
        print(
            f"[Payment:Callback] STEP 2 — PaymentResponseCode not resolved ({payg_response_code_raw!r}); "
            f"calling PayG Detail API (oid={oid!r} rid={rid!r} key={order_key_id_from_cb!r})..."
        )
        enriched = _enrich_params_via_detail_api(oid, rid, order_key_id=order_key_id_from_cb)
        if enriched and enriched.get("PaymentResponseCode") not in (None, "", -1):
            params = enriched
            print(
                f"[Payment:Callback] STEP 2 ✅ Detail API resolved | "
                f"code={params.get('PaymentResponseCode')!r} | status={params.get('OrderStatus')!r}"
            )
        else:
            # Detail API failed (likely 403 IP not whitelisted) or returned code=0/4.
            # CRITICAL: If PayG sent PaymentResponseCode directly in the redirect
            # params (even as 0 or 4), trust that over the Detail API failure.
            if payg_response_code_direct is not None:
                print(
                    f"[Payment:Callback] STEP 2 ⚠️  Detail API failed but PayG gave "
                    f"code={payg_response_code_direct} directly in redirect — using that"
                )
                # params already has the direct code — no change needed
            else:
                print(
                    f"[Payment:Callback] STEP 2 ❌ Detail API returned nothing AND PayG "
                    f"gave no code in redirect params — code defaults to -1 (UNKNOWN).\n"
                    f"  If this keeps happening: log into PayG portal and add this\n"
                    f"  server's public IP to the IP Whitelist so the Detail API works."
                )
    elif payg_gave_us_params:
        print(
            f"[Payment:Callback] STEP 2 — Skipped Detail API — "
            f"PayG already gave PaymentResponseCode={payg_response_code_raw!r} in redirect params"
        )
    else:
        print(
            f"[Payment:Callback] STEP 2 ❌ Cannot call Detail API — "
            f"no oid, rid, or OrderKeyId available in params"
        )

    # ── STEP 3: Find order in DB ──────────────────────────────────────────
    order_id_str  = params.get("UserDefined1") or oid or ""
    order_key_id  = params.get("OrderKeyId", "")
    unique_req_id = params.get("UniqueRequestId", "") or rid

    print(
        f"[Payment:Callback] STEP 3 — Looking up order | "
        f"order_id_str={order_id_str!r} | order_key_id={order_key_id!r} | unique_req_id={unique_req_id!r}"
    )

    order: Optional[Orders] = None
    if order_id_str:
        try:
            order = db.query(Orders).filter(
                Orders.orderId    == int(order_id_str),
                Orders.deletedInd == False,
            ).first()
            print(f"[Payment:Callback] STEP 3 — Lookup by orderId={order_id_str!r}: {'FOUND' if order else 'NOT FOUND'}")
        except Exception as e:
            print(f"[Payment:Callback] STEP 3 ⚠️  orderId lookup error: {e}")

    if order is None and order_key_id:
        order = db.query(Orders).filter(
            Orders.orderKeyId  == order_key_id,
            Orders.deletedInd  == False,
        ).first()
        print(f"[Payment:Callback] STEP 3 — Lookup by orderKeyId={order_key_id!r}: {'FOUND' if order else 'NOT FOUND'}")

    if order is None and unique_req_id:
        order = db.query(Orders).filter(
            Orders.paymentTransactionRefNo == unique_req_id,
            Orders.deletedInd              == False,
        ).first()
        print(f"[Payment:Callback] STEP 3 — Lookup by uniqueRequestId={unique_req_id!r}: {'FOUND' if order else 'NOT FOUND'}")

    if order is None:
        print(
            f"[Payment:Callback] STEP 3 ❌ ORDER NOT FOUND in this Azure DB!\n"
            f"   Tried: orderId={order_id_str!r} | orderKeyId={order_key_id!r} | uniqueReqId={unique_req_id!r}\n"
            f"   ROOT CAUSE: Frontend called a LOCAL backend (localhost:8000) to place the order,\n"
            f"               but PayG is calling back to this Azure instance — the order is NOT here.\n"
            f"   FIX 1: Set VITE_API_BASE_URL in frontend/.env.local → this Azure URL.\n"
            f"   FIX 2: Add http://localhost:5173 to CORS_ORIGINS env var in Azure."
        )
        return RedirectResponse(
            url=f"{_frontend_url()}/user-products/payment-callback?status=error&message=Order+not+found",
            status_code=302,
        )

    order_id = order.orderId
    print(
        f"[Payment:Callback] STEP 3 ✅ Order found | "
        f"id={order_id} | number={order.orderNumber!r} | state={order.state!r} | "
        f"orderKeyId={order.orderKeyId!r} | paymentRef={order.paymentTransactionRefNo!r}"
    )

   # ── STEP 4: Hash verification skipped ────────────────────────────────
# PayG does not send HashData in the callback redirect.
# Payment outcome is determined solely by the Detail API response in STEP 2.

    # ── STEP 5: Determine payment outcome ─────────────────────────────────
    try:
        response_code = int(params.get("PaymentResponseCode", -1))
    except (ValueError, TypeError):
        response_code = -1

    success = (response_code == 1)
    new_state = payg_client.payment_code_to_order_state(response_code)
    print(
        f"[Payment:Callback] STEP 5 — Payment outcome | "
        f"response_code={response_code} | success={success} | new_state={new_state!r} | "
        f"PaymentMethod={params.get('PaymentMethod')!r} | "
        f"PaymentTransactionId={params.get('PaymentTransactionId')!r}"
    )

    # ── STEP 6: Update order state in DB ──────────────────────────────────
    # BUG FIX: Do NOT call _update_order_after_payment when response_code=-1
    # (unknown — Detail API failed even after retries).  Calling it would set
    # order.state = "AwaitingPayment" which is fine, but we must not wipe out a
    # state that may have already been set to "Confirmed" by a prior callback
    # (duplicate redirect scenario).  When code=-1, leave the order exactly as
    # it is in the DB and let the frontend polling resolve it.
    print(f"[Payment:Callback] STEP 6 — Updating order {order_id} state → {new_state!r} in DB...")
    if response_code == -1:
        print(
            f"[Payment:Callback] STEP 6 ℹ️  code=-1 (unknown) — "
            f"skipping state update; order stays as '{order.state}' until resolved"
        )
    else:
        try:
            _update_order_after_payment(db, order, response_code, params)
            _record_payment_log(db, order_id, order_key_id, unique_req_id, params)
            db.commit()
            print(f"[Payment:Callback] STEP 6 ✅ DB updated → state={new_state!r} for order {order_id}")
        except Exception as exc:
            db.rollback()
            print(f"[Payment:Callback] STEP 6 ❌ DB update FAILED for order {order_id}: {exc}")

    # ── STEP 7: On success — remove cart items + queue Ekart shipment ────────
    #
    # PayG PaymentResponseCode meanings:
    #   1  → Approved (definitive success)
    #   2  → Declined (definitive failure — bank rejected)
    #   0  → Initiated / Pending (payment not settled yet — DO NOT destroy order)
    #   4  → Payment Pending    (payment not settled yet — DO NOT destroy order)
    #  -1  → Unknown (Detail API failed or timed out   — DO NOT destroy order)
    #
    # ── BUG FIX: Previously the `else` branch ran for ALL non-success codes
    # (0, 4, -1, 2) and soft-deleted OrderItems.  This destroyed the order
    # when PayG returned code=0/4 (payment still in-flight) or when the
    # Detail API itself failed (code=-1, e.g. network timeout, wrong
    # credentials).  The user then saw "Payment Not Completed / declined by
    # the bank" even though their money had already left their account.
    #
    # The fix: only soft-delete OrderItems when response_code==2, which is
    # the ONLY code that means the bank definitively rejected the transaction.
    # For codes 0, 4 (pending) and -1 (unknown), leave the order and its
    # items intact — the /Payment/Status poll or the Expire endpoint will
    # clean up if the payment truly never settles.
    DEFINITIVE_FAILURE = (response_code == 2)  # 2 = bank declined

    if success:
        print(
            f"[Payment:Callback] STEP 7 — Payment SUCCESS ✅ | "
            f"Queueing _auto_create_ekart_shipment background task for order {order_id}..."
        )
        background_tasks.add_task(_auto_create_ekart_shipment, order_id)
        print(f"[Payment:Callback] STEP 7 ✅ Background task QUEUED — watch for [Payment→Ekart] logs next")

        # Remove all ordered cart items completely so the cart is clean for the user.
        # The corrected query uses a CTE to collect exactly the CartIds that
        # were ordered (by matching PVDID through OrderItems), then deletes only
        # those rows.  One CartId per PVDID is selected (oldest first, i.e. the
        # row that was most likely in the cart when the order was placed).
        print(f"[Payment:Callback] STEP 7 — Removing ordered cart items for order {order_id}...")
        try:
            result = db.execute(text("""
                WITH user_id AS (
                    SELECT "UserProfileId"
                    FROM twam."Orders"
                    WHERE "OrderId" = :oid AND "DeletedInd" = false
                    LIMIT 1
                ),
                carts_to_clear AS (
                    SELECT DISTINCT ON (c."ProductVariantDetailId") c."CartId"
                    FROM twam."Cart" c
                    INNER JOIN twam."OrderItems" oi
                        ON oi."OrderId"               = :oid
                        AND oi."DeletedInd"            = false
                        AND c."ProductVariantDetailId" = oi."ProductVariantDetailId"
                    WHERE c."UserProfileId" = (SELECT "UserProfileId" FROM user_id)
                      AND c."DeletedInd"    = false
                    ORDER BY c."ProductVariantDetailId", c."CreatedDate" ASC
                )
                UPDATE twam."Cart"
                SET "DeletedInd"   = true,
                    "IsOrdered"    = true,
                    "ModifiedDate" = :now
                WHERE "CartId" IN (SELECT "CartId" FROM carts_to_clear)
            """), {"oid": order_id, "now": datetime.now(timezone.utc)})
            db.commit()
            print(
                f"[Payment:Callback] STEP 7 ✅ Cart cleared — "
                f"{result.rowcount} item(s) removed for order {order_id}"
            )
        except Exception as exc:
            db.rollback()
            print(f"[Payment:Callback] STEP 7 ⚠️  Cart removal FAILED (non-fatal) for order {order_id}: {exc}")
    elif DEFINITIVE_FAILURE:
        # code=2: bank definitively declined — safe to clean up OrderItems
        # so they never bleed into a future invoice.  Cart items are kept so
        # the user can retry with the same products.
        print(
            f"[Payment:Callback] STEP 7 — Payment DECLINED by bank (code=2) | "
            f"Soft-deleting OrderItems for order {order_id}..."
        )
        try:
            cleanup = db.execute(text("""
                UPDATE twam."OrderItems"
                SET    "DeletedInd"   = true,
                       "ModifiedDate" = :now
                WHERE  "OrderId"    = :oid
                  AND  "DeletedInd" = false
            """), {"oid": order_id, "now": datetime.now(timezone.utc)})
            db.commit()
            print(
                f"[Payment:Callback] STEP 7 ✅ Soft-deleted {cleanup.rowcount} OrderItem(s) "
                f"for declined order {order_id}"
            )
        except Exception as exc:
            db.rollback()
            print(f"[Payment:Callback] STEP 7 ⚠️  OrderItems cleanup failed for order {order_id}: {exc}")
    else:
        # code=0 (Initiated), code=4 (Pending), code=-1 (Detail API failed/unknown)
        # Payment outcome is NOT yet known — preserve order and items intact.
        # The frontend /Payment/Status poll or the 10-min Expire endpoint will
        # resolve the order once PayG settles the transaction.
        print(
            f"[Payment:Callback] STEP 7 — Payment outcome AMBIGUOUS (code={response_code}) | "
            f"Preserving order {order_id} and its OrderItems intact — NOT deleting. "
            f"Frontend status-poll or Expire endpoint will resolve."
        )

    # ── STEP 8: Send notification ─────────────────────────────────────────
    # BUG FIX: Previously a failure notification was sent for code=-1 (unknown)
    # because success=False.  This wrongly told the customer "your payment failed"
    # even when their money had already been debited.
    # Rule: only send notifications for DEFINITIVE outcomes.
    #   code=1  → success notification (order confirmed)
    #   code=2  → failure notification (bank declined)
    #   code=0/4/-1 → NO notification yet; the Status poll / admin will resolve it
    print(f"[Payment:Callback] STEP 8 — Payment notification | response_code={response_code}")
    if response_code == 1:
        _send_payment_notification(db, order_id, success=True)
        print(f"[Payment:Callback] STEP 8 ✅ Success notification sent for order {order_id}")
    elif response_code == 2:
        _send_payment_notification(db, order_id, success=False)
        print(f"[Payment:Callback] STEP 8 ✅ Failure notification sent for order {order_id}")
    else:
        print(
            f"[Payment:Callback] STEP 8 ℹ️  code={response_code} (ambiguous) — "
            f"skipping notification; will send once Status poll resolves the outcome"
        )

    # ── STEP 9: Redirect user ─────────────────────────────────────────────
    #
    # code=1 (success)   → /order-confirm/{id}?payment=success
    # code=2 (declined)  → /payment-callback?status=failed&reason=declined
    # code=0/4 (pending) → /order-confirm/{id}?payment=pending   (frontend polls)
    # code=-1 (unknown)  → /order-confirm/{id}?payment=pending   (frontend polls)
    #
    # BUG FIX: Previously codes 0, 4, -1 all redirected to the "Payment Not
    # Completed" failure page, which destroyed the user's confidence even though
    # their money may have already left their account.  For ambiguous outcomes
    # we now redirect to order-confirm with ?payment=pending so the frontend
    # can poll /Payment/Status/{orderId} every few seconds and resolve the true
    # outcome without the user seeing a false "declined" screen.
    if success:
        redirect_url = f"{_frontend_url()}/user-products/order-confirm/{order_id}?payment=success"

    elif response_code == 2:
        # Definitive bank decline — safe to show failure page
        reason = params.get("PaymentReasonCode") or "declined"
        redirect_url = (
            f"{_frontend_url()}/user-products/payment-callback"
            f"?status=failed&orderId={order_id}&code={response_code}&reason={reason}"
        )
    else:
        # code=0, 4, -1: ambiguous — send to order-confirm with pending flag.
        # The frontend polls /Payment/Status until the outcome is known.
        redirect_url = f"{_frontend_url()}/user-products/order-confirm/{order_id}?payment=pending"

    print(f"[Payment:Callback] STEP 9 — Redirecting → {redirect_url}")
    print(f"{SEP}\n")
    return RedirectResponse(url=redirect_url, status_code=302)


@router.get("/debug-settings")
def debug_settings():
    return {
        "frontend_base_url_raw": get_setting("frontend_base_url", "NOT_IN_DB"),
        "frontend_url_fn": _frontend_url(),
        "env_var": os.getenv("FRONTEND_BASE_URL", "NOT_SET"),
    }

# ── PayG Server-to-Server (S2S) Webhook ───────────────────────────────────────
#
# PayG can POST the final payment result directly from their servers to this
# endpoint SEPARATELY from the browser redirect.  This is more reliable than
# the browser redirect because:
#   1. It is not affected by the user closing the browser tab
#   2. It arrives from PayG's server IP (which can be whitelisted separately)
#   3. It always carries the final PaymentResponseCode (1 or 2)
#
# Configure this URL in the PayG portal:
#   UAT:  https://uat.payg.in/merchant/#/login → Settings → Webhook URL
#   Live: https://payg.in/merchant/#/login  → Settings → Webhook URL
#   Set to: https://<your-backend-domain>/api/Payment/Webhook
#
# This endpoint updates the order state and sends the success notification if
# the callback endpoint missed it (code=-1 scenario).

@router.post("/Webhook", include_in_schema=True)
async def payment_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    PayG server-to-server webhook. PayG POSTs final payment status here.
    No browser redirect — just update the DB and return 200.
    """
    SEP = "=" * 60
    print(f"\n{SEP}")
    print(f"[Payment:Webhook] ▶ Received S2S webhook from PayG")

    # Parse body (JSON or form)
    params: dict = {}
    params.update(dict(request.query_params))
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        try:
            body = await request.json()
            if isinstance(body, dict):
                params.update(body)
        except Exception:
            pass
    else:
        try:
            form = await request.form()
            params.update(dict(form))
        except Exception:
            pass

    print(f"[Payment:Webhook] Raw params={json.dumps(params, default=str)}")

    try:
        response_code = int(params.get("PaymentResponseCode", -1))
    except (ValueError, TypeError):
        response_code = -1

    # Find the order
    order_id_str  = params.get("UserDefined1", "") or params.get("oid", "")
    order_key_id  = params.get("OrderKeyId", "")
    unique_req_id = params.get("UniqueRequestId", "")

    order = None
    if order_id_str:
        try:
            order = db.query(Orders).filter(
                Orders.orderId    == int(order_id_str),
                Orders.deletedInd == False,
            ).first()
        except (ValueError, TypeError):
            pass
    if order is None and order_key_id:
        order = db.query(Orders).filter(
            Orders.orderKeyId == order_key_id,
            Orders.deletedInd == False,
        ).first()
    if order is None and unique_req_id:
        order = db.query(Orders).filter(
            Orders.paymentTransactionRefNo == unique_req_id,
            Orders.deletedInd             == False,
        ).first()

    if order is None:
        print(f"[Payment:Webhook] ❌ Order not found — UserDefined1={order_id_str!r} key={order_key_id!r}")
        return {"status": "order_not_found"}

    order_id = order.orderId
    was_confirmed = (order.state == "Confirmed")
    new_state     = payg_client.payment_code_to_order_state(response_code)
    success       = (response_code == 1)

    print(
        f"[Payment:Webhook] order={order_id} | code={response_code} | "
        f"new_state={new_state!r} | was_confirmed={was_confirmed}"
    )

    # Only update if this is a definitive result (1 or 2) and not already confirmed
    if response_code in (1, 2) and not was_confirmed:
        try:
            _update_order_after_payment(db, order, response_code, params)
            _record_payment_log(db, order_id, order_key_id, unique_req_id, params)
            db.commit()
            print(f"[Payment:Webhook] ✅ Order {order_id} updated → {new_state!r}")
        except Exception as exc:
            db.rollback()
            print(f"[Payment:Webhook] ❌ DB update failed for order {order_id}: {exc}")

    # Send notification (deferred from callback when code was -1)
    if success and not was_confirmed:
        background_tasks.add_task(_auto_create_ekart_shipment, order_id)
        _send_payment_notification(db, order_id, success=True)
        print(f"[Payment:Webhook] ✅ Success notification + Ekart shipment queued for order {order_id}")
    elif response_code == 2 and not was_confirmed:
        _send_payment_notification(db, order_id, success=False)
        print(f"[Payment:Webhook] ✅ Failure notification sent for order {order_id}")

    print(f"{SEP}\n")
    return {"status": "ok", "orderId": order_id, "state": new_state}


# ── Admin: manually confirm a payment ────────────────────────────────────────
# For cases where PayG S2S webhook was not configured and the callback failed.
# Allows support staff to mark an order as Confirmed after verifying payment
# in the PayG dashboard.

@router.post("/AdminConfirm/{order_id}", include_in_schema=True)
def admin_confirm_payment(
    order_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Staff-only: manually confirm a payment that was stuck in AwaitingPayment
    because the Detail API was IP-blocked and the S2S webhook was not set up.
    """
    if current_user.role_id not in Roles.STAFF:
        raise HTTPException(403, "Staff only")

    order = db.query(Orders).filter(
        Orders.orderId    == order_id,
        Orders.deletedInd == False,
    ).first()
    if not order:
        raise HTTPException(404, "Order not found")

    SAFE_STATES = {"AwaitingPayment", "Pending", "Payment Pending", "AwaitingPayment"}
    if order.state not in SAFE_STATES:
        return {
            "success": False,
            "message": f"Order is already in state '{order.state}' — no change made",
            "state": order.state,
        }

    order.state        = "Confirmed"
    order.modifiedDate = datetime.now(timezone.utc)
    db.add(order)
    db.commit()

    background_tasks.add_task(_auto_create_ekart_shipment, order_id)
    _send_payment_notification(db, order_id, success=True)

    print(f"[Payment:AdminConfirm] ✅ Order {order_id} manually confirmed by staff {current_user.user_id}")
    return {"success": True, "orderId": order_id, "state": "Confirmed"}


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
async def get_payment_status(
    order_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    # FIX: use get_current_user_optional instead of get_current_user.
    # get_current_user raises HTTPException when no token is present;
    # Optional[CurrentUser] on its own does NOT suppress that raise.
    # After a PayG redirect the browser session/cookie is typically lost,
    # so every poll would return 403 without this wrapper.
    current_user: Optional[CurrentUser] = Depends(get_current_user_optional),
):
    order = db.query(Orders).filter(
        Orders.orderId    == order_id,
        Orders.deletedInd == False,
    ).first()
    if not order:
        raise HTTPException(404, "Order not found")

    # Only enforce ownership check if user is authenticated.
    # Unauthenticated polls are allowed — PayG redirect may lose session.
    if current_user and current_user.role_id not in Roles.STAFF:
        if order.userProfileId != current_user.user_id:
            raise HTTPException(403, "Not authorised")

    local_state = order.state or "AwaitingPayment"

    # ── UPI Intent / no-callback flows: actively poll PayG ────────────────
    # Card payments get resolved via /Callback (browser redirect from PayG).
    # UPI Intent has no browser redirect — the user leaves to a UPI app and
    # nothing ever hits our server, so order.state stays "AwaitingPayment"
    # forever unless we actively ask PayG for the live status here.
    PENDING_STATES = {"AwaitingPayment", "Pending", "Payment Pending"}
    if local_state in PENDING_STATES:
        try:
            enriched = _enrich_params_via_detail_api(
                oid=str(order_id),
                rid=order.paymentTransactionRefNo or "",
                order_key_id=order.orderKeyId or "",
            )
            response_code = enriched.get("PaymentResponseCode", -1)
            print(
                f"[Payment/Status] Live PayG check for order {order_id} → "
                f"code={response_code!r} (was '{local_state}')"
            )

            # PayG codes: 1=Approved, 2=Declined, 4/None=Pending → only act on 1/2
            if response_code in (1, 2):
                _record_payment_log(
                    db, order_id,
                    enriched.get("OrderKeyId", order.orderKeyId or ""),
                    enriched.get("UniqueRequestId", order.paymentTransactionRefNo or ""),
                    enriched,
                )
                _update_order_after_payment(db, order, int(response_code), enriched)
                db.commit()
                local_state = order.state

                success = (response_code == 1)
                _send_payment_notification(db, order_id, success)
                if success:
                    background_tasks.add_task(_auto_create_ekart_shipment, order_id)

                print(f"[Payment/Status] Order {order_id} resolved → {local_state}")
        except Exception as exc:
            # Don't fail the poll — just log and return current (still pending) state.
            print(f"[Payment/Status] Live PayG check FAILED for order {order_id}: {exc}")

    if local_state == "Confirmed":
        try:
            from app.ekart.notifications import send_order_placed_notification
            send_order_placed_notification(db, order_id)
        except Exception as exc:
            print(f"[Payment/Status] Deferred notification error: {exc}")

    return {
        "orderId":              order_id,
        "orderNumber":          order.orderNumber,
        "state":                local_state,
        "paymentMethod":        order.paymentMethod,
        "paymentTransactionId": order.paymentTransactionId,
        "paymentResponseCode":  None,
        "paygStatus":           None,
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

    # ── FIX: Soft-delete OrderItems from this failed order so they can never
    # bleed into a subsequent order's item list or invoice display.
    # Cart items are intentionally left intact (IsOrdered=false) so the user
    # can retry from their cart — they are re-fetched by CartId on the next
    # checkout, not by scanning all unordered items.
    try:
        result = db.execute(text("""
            UPDATE twam."OrderItems"
            SET    "DeletedInd"   = true,
                   "ModifiedDate" = :now
            WHERE  "OrderId"    = :oid
              AND  "DeletedInd" = false
        """), {"oid": order_id, "now": datetime.now(timezone.utc)})
        db.commit()
        print(
            f"[Payment/Expire] Soft-deleted {result.rowcount} OrderItem(s) "
            f"for failed order {order_id}"
        )
    except Exception as exc:
        db.rollback()
        print(f"[Payment/Expire] OrderItems cleanup failed for order {order_id}: {exc}")

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


@router.get("/OutboundIp")
def get_outbound_ip():
    """
    Returns this server's public outbound IP address.
    Share this IP with PayG support to get it whitelisted.
    No auth required — safe to call from browser/Postman.
    """
    import httpx as _httpx
    results = {}
    for svc, url in [
        ("amazonaws",  "https://checkip.amazonaws.com"),
        ("ipify",      "https://api.ipify.org"),
        ("my-ip.io",   "https://api4.my-ip.io/ip"),
    ]:
        try:
            r = _httpx.get(url, timeout=5)
            results[svc] = r.text.strip()
            break
        except Exception as e:
            results[svc] = f"failed: {e}"
    return {"outbound_ip": results}


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
        frontend_base_url    = get_setting("frontend_base_url"),
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