"""
app/payment/router.py
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


async def get_current_user_optional(
    request: Request,
    db: Session = Depends(get_db),
) -> Optional[CurrentUser]:
    try:
        return await get_current_user(request, db)
    except Exception:
        return None


def _frontend_url() -> str:
    return get_setting("frontend_base_url", "NOT_FOUND") or "NOT_FOUND"


def _record_payment_log(db, order_id, order_key_id, unique_request_id, payload):
    try:
        merch_key_id_str = get_setting("payg_merchant_key_id", "0")
        merch_key_id = int(merch_key_id_str) if merch_key_id_str.isdigit() else 0
        log = OrderRefund(
            orderId=order_id, orderKeyId=order_key_id,
            merchanKeyId=merch_key_id, uniqueRequestId=unique_request_id,
            orderStatus=payload.get("OrderStatus", ""),
            paymentStatus=payload.get("PaymentStatus"),
            paymentTransactionId=payload.get("PaymentTransactionId", ""),
            paymentResponseCode=payload.get("PaymentResponseCode"),
            paymentReasonCode=payload.get("PaymentReasonCode", ""),
            paymentTransactionRefNo=payload.get("PaymentTransactionRefNo", ""),
            paymentMethod=payload.get("PaymentMethod", ""),
            paymentAccount=payload.get("PaymentAccount", ""),
            createdDate=datetime.now(timezone.utc),
            modifiedDate=datetime.now(timezone.utc),
            deletedInd=False,
        )
        db.add(log)
    except Exception as exc:
        print(f"[Payment] OrderRefund log failed for order {order_id}: {exc}")


def _update_order_after_payment(db, order, response_code, payload):
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
        orderId=order.orderId, status=new_state,
        createdDate=datetime.now(timezone.utc),
    ))


def _send_payment_notification(db, order_id, success):
    try:
        if success:
            from app.ekart.notifications import send_order_placed_notification
            send_order_placed_notification(db, order_id)
        else:
            from app.ekart.notifications import send_payment_failure_notification
            send_payment_failure_notification(db, order_id)
    except Exception as exc:
        print(f"[Payment] Notification error for order {order_id}: {exc}")


def _enrich_params_via_detail_api(oid, rid, order_key_id=""):
    detail = {}
    try:
        if order_key_id:
            try:
                detail = payg_client.get_payg_order_detail(order_key_id=order_key_id)
                print(f"[Payment] Detail API (OrderKeyId) -> code={detail.get('PaymentResponseCode')} status={detail.get('OrderStatus')}")
            except Exception as exc_key:
                print(f"[Payment] Warning: Detail API failed with OrderKeyId: {exc_key}")
                if rid:
                    print(f"[Payment] Retrying with UniqueRequestId={rid!r}...")
                    detail = payg_client.get_payg_order_detail(unique_request_id=rid)
                    print(f"[Payment] Detail API (UniqueRequestId) -> code={detail.get('PaymentResponseCode')} status={detail.get('OrderStatus')}")
                else:
                    raise
        elif rid:
            detail = payg_client.get_payg_order_detail(unique_request_id=rid)
            print(f"[Payment] Detail API (UniqueRequestId) -> code={detail.get('PaymentResponseCode')} status={detail.get('OrderStatus')}")
        else:
            print("[Payment] Detail API skipped -- no rid or order_key_id available")
            return {}
    except Exception as exc:
        print(f"[Payment] ❌ Detail API FAILED for ref={rid or order_key_id}: {exc}")
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


def _mark_brand_coupon_used_if_applicable(db, order_id: int) -> None:
    """
    If this order used a brand coupon, mark it as used.
    Called after payment confirmed, alongside cart clearing. Non-fatal.
    """
    try:
        order = db.query(Orders).filter(
            Orders.orderId == order_id, Orders.deletedInd == False
        ).first()
        if not order or not order.couponId:
            return
        row = db.execute(text("""
            SELECT "CouponId" FROM coupons."Coupons"
            WHERE "CouponId" = :cid AND "CouponType" = 'brand_specific' AND "DeletedInd" = false
        """), {"cid": order.couponId}).fetchone()
        if not row:
            return
        from app.coupons.repository import mark_brand_coupon_used
        mark_brand_coupon_used(db, coupon_id=order.couponId,
                               user_profile_id=order.userProfileId or "", order_id=order_id)
        print(f"[Payment] ✅ Brand coupon {order.couponId} marked used for order {order_id}")
    except Exception as exc:
        print(f"[Payment] ⚠️  Brand coupon mark-used failed for order {order_id}: {exc}")


def _clear_ordered_cart_items(db, order_id, user_profile_id=""):
    """Marks ordered cart items as deleted. Called from all payment success paths."""
    try:
        if user_profile_id:
            result = db.execute(text("""
                WITH carts_to_clear AS (
                    SELECT DISTINCT ON (c."ProductVariantDetailId") c."CartId"
                    FROM twam."Cart" c
                    INNER JOIN twam."OrderItems" oi
                        ON oi."OrderId" = :oid AND oi."DeletedInd" = false
                        AND c."ProductVariantDetailId" = oi."ProductVariantDetailId"
                    WHERE c."UserProfileId" = :uid AND c."DeletedInd" = false
                    ORDER BY c."ProductVariantDetailId", c."CreatedDate" ASC
                )
                UPDATE twam."Cart"
                SET "DeletedInd" = true, "IsOrdered" = true, "ModifiedDate" = :now
                WHERE "CartId" IN (SELECT "CartId" FROM carts_to_clear)
            """), {"oid": order_id, "uid": user_profile_id, "now": datetime.now(timezone.utc)})
        else:
            result = db.execute(text("""
                WITH user_id AS (
                    SELECT "UserProfileId" FROM twam."Orders"
                    WHERE "OrderId" = :oid AND "DeletedInd" = false LIMIT 1
                ),
                carts_to_clear AS (
                    SELECT DISTINCT ON (c."ProductVariantDetailId") c."CartId"
                    FROM twam."Cart" c
                    INNER JOIN twam."OrderItems" oi
                        ON oi."OrderId" = :oid AND oi."DeletedInd" = false
                        AND c."ProductVariantDetailId" = oi."ProductVariantDetailId"
                    WHERE c."UserProfileId" = (SELECT "UserProfileId" FROM user_id)
                      AND c."DeletedInd" = false
                    ORDER BY c."ProductVariantDetailId", c."CreatedDate" ASC
                )
                UPDATE twam."Cart"
                SET "DeletedInd" = true, "IsOrdered" = true, "ModifiedDate" = :now
                WHERE "CartId" IN (SELECT "CartId" FROM carts_to_clear)
            """), {"oid": order_id, "now": datetime.now(timezone.utc)})
        db.commit()
        print(f"[Payment] Cart cleared — {result.rowcount} item(s) removed for order {order_id}")
        return result.rowcount
    except Exception as exc:
        db.rollback()
        print(f"[Payment] Cart cleanup FAILED for order {order_id}: {exc}")
        return 0


def _send_ekart_failure_alert(db, order, items, error_message):
    try:
        from app.sendemail.repository import send_email
        from app.sendemail.schemas import EmailCreate
        support_email = get_setting("SUPPORT_EMAIL", "customersupport@onlytwam.com")
        subject = f"[ACTION NEEDED] Ekart shipment failed — Order #{order['order_number']}"
        body = f"""
<h3>Ekart Shipment Failed — Manual Action Required</h3>
<p><b>Order:</b> #{order['order_number']} (ID: {order['order_id']})</p>
<p><b>Error:</b> {error_message}</p>
<p><b>Total:</b> ₹{order['total_amount']:.2f} | <b>Payment Mode:</b> {order.get('payment_mode','Prepaid')}</p>
<h4>Consignee Details</h4>
<ul>
  <li><b>Name:</b> {order.get('name','')}</li>
  <li><b>Phone:</b> {order.get('phone','')}</li>
  <li><b>Address:</b> {order.get('address_line','')}, {order.get('city','')}, {order.get('state_name','')}, {order.get('pincode','')}</li>
</ul>
<h4>Items</h4>
<ul>{''.join(f"<li>{i.name} — qty {i.quantity} — ₹{i.price} — SKU: {i.sku or 'N/A'} — HSN: {i.hsn_code or 'N/A'}</li>" for i in items)}</ul>
<p>Enter the AWB from the Ekart portal in Admin → Failed Shipments to send tracking email to customer.</p>
"""
        send_email(db, EmailCreate(
            name="Customer Support", email=support_email,
            subject=subject, message=body,
        ))
        print(f"[Payment→Ekart] ✅ Failure alert email sent to {support_email} for order {order['order_id']}")
    except Exception as exc:
        print(f"[Payment→Ekart] ❌ Failure alert email FAILED for order {order['order_id']}: {exc}")


async def _auto_create_ekart_shipment(order_id: int) -> None:
    from database import SessionLocal
    db = SessionLocal()
    SEP = "─" * 60
    try:
        print(f"\n{SEP}")
        print(f"[Payment→Ekart] ▶▶▶ START auto-shipment for order={order_id}")

        from app.ekart.client import get_ekart_client_for_db, load_ekart_config_from_db
        from app.ekart.schemas import CreateShipmentRequest, EkartAddress, EkartShipmentItem
        from app.ekart.notifications import send_order_shipped_notification

        cfg = load_ekart_config_from_db(db)
        if cfg is None:
            print(f"[Payment→Ekart] STEP 1 ❌ No EKart config found.")
            return
        print(f"[Payment→Ekart] STEP 1 ✅ Config loaded | client_id={cfg.client_id!r} | complete={cfg.is_complete}")

        row = db.execute(text("""
            SELECT o."OrderId", o."OrderNumber", o."TotalAmount", o."State",
                   o."TrackingId", o."PaymentMethod", o."DeliveryCharge",
                   a."Name", a."Phone", a."AddressLine", a."City",
                   a."PinCode", s."StateName", c."CountryName",
                   p."EmailId", o."ShippingTypeId", st."ShippingTypeName",
                   o."ShippingAddressId"
            FROM twam."Orders" o
            LEFT JOIN twam."Address" a   ON a."AddressId"     = o."ShippingAddressId"
            LEFT JOIN mdm."State" s      ON s."StateId"       = a."StateId"
            LEFT JOIN mdm."Country" c    ON c."CountryId"     = a."CountryId"
            LEFT JOIN twam."People" p    ON p."UserProfileId" = o."UserProfileId"
            LEFT JOIN mdm."ShippingType" st ON st."ShippingTypeId" = o."ShippingTypeId" AND st."DeletedInd" = false
            WHERE o."OrderId" = :oid AND o."DeletedInd" = false
        """), {"oid": order_id}).fetchone()

        if not row:
            print(f"[Payment→Ekart] STEP 2 ❌ Order {order_id} NOT IN DB!")
            return

        order = {
            "order_id": row[0], "order_number": row[1] or str(row[0]),
            "total_amount": float(row[2]) if row[2] else 0,
            "state": row[3], "tracking_id": row[4],
            "payment_mode": row[5] if row[5] else "Prepaid",
            "delivery_charge": float(row[6]) if row[6] else 0,
            "name": row[7] or "Customer", "phone": row[8] or "",
            "address_line": row[9] or "", "city": row[10] or "",
            "state_name": row[12] or "", "pincode": row[11] or "",
            "country": row[13] or "India", "email": row[14],
            "shipping_type_name": row[16] or "Normal",
            "shipping_address_id": row[17],
        }
        print(f"[Payment→Ekart] STEP 2 ✅ Order found | state={order['state']!r} | tracking={order['tracking_id']!r}")

        if order["tracking_id"]:
            print(f"[Payment→Ekart] STEP 3 ⚠️  Already has tracking_id — skipping"); return
        if order["state"] in ("Shipped", "Delivered", "Cancelled"):
            print(f"[Payment→Ekart] STEP 3 ⚠️  State '{order['state']}' — skipping"); return
        print(f"[Payment→Ekart] STEP 3 ✅ Guards passed")

        item_rows = db.execute(text("""
            SELECT p."Name", COALESCE(pvd."StockId", p."ProductCode", CAST(oi."ProductId" AS TEXT)) AS sku,
                   oi."Quantity", oi."Price", thc."HSNCode"
            FROM twam."OrderItems" oi
            LEFT JOIN twam."Products" p         ON p."ProductId"         = oi."ProductId"
            LEFT JOIN twam."ProductVariants" pv ON pv."ProductVariantId" = oi."ProductVariantId"
            LEFT JOIN twam."ProductVariantDetail" pvd ON pvd."ProductVariantDetailId" = oi."ProductVariantDetailId"
            LEFT JOIN mdm."TaxHSNCode" thc      ON thc."TaxHSNCodeId"    = p."TaxHSNCodeId"
            WHERE oi."OrderId" = :oid AND oi."DeletedInd" = false
        """), {"oid": order_id}).fetchall()

        if not item_rows:
            print(f"[Payment→Ekart] STEP 4 ❌ No OrderItems found!"); return

        items = [EkartShipmentItem(
            name=r[0] or "Product", sku=r[1],
            quantity=r[2] or 1, price=float(r[3]) if r[3] else 0, hsn_code=r[4],
        ) for r in item_rows]
        print(f"[Payment→Ekart] STEP 4 ✅ {len(items)} item(s)")

        try:
            from app.surprisegift.repository import get_order_gift_snapshot
            gift_snap = get_order_gift_snapshot(db, order_id)
            if gift_snap and gift_snap.giftType == "physical_gift":
                items.append(EkartShipmentItem(
                    name=gift_snap.giftName, sku=gift_snap.sku or f"GIFT-{gift_snap.giftRuleId}",
                    quantity=1, price=0.0, hsn_code=gift_snap.hsnCode,
                ))
        except Exception as _gexc:
            print(f"[Payment→Ekart] STEP 5 ⚠️  Surprise gift check failed (non-fatal): {_gexc}")

        request = CreateShipmentRequest(
            order_id=order["order_id"], order_number=order["order_number"],
            consignee_name=order["name"], consignee_phone=order["phone"],
            consignee_address=EkartAddress(
                name=order["name"], phone=order["phone"],
                address_line1=order["address_line"], city=order["city"],
                state=order["state_name"], pincode=order["pincode"], country=order["country"],
            ),
            payment_mode="COD" if order["payment_mode"] == "COD" else "Prepaid",
            cod_amount=order["total_amount"] if order["payment_mode"] == "COD" else None,
            total_amount=order["total_amount"], items=items,
            shipping_type_name=order.get("shipping_type_name", "Normal"),
        )

        client = get_ekart_client_for_db(db)
        print(f"[Payment→Ekart] STEP 7 — Client mode: {'MOCK ⚠️' if client.mock_mode else 'LIVE ✅'}")
        if client.mock_mode:
            print(f"[Payment→Ekart] STEP 7 ❌ MOCK MODE — no AWB generated."); return

        result = await client.create_shipment(request)
        if not result.success:
            print(f"[Payment→Ekart] STEP 8 ❌ FAILED — {result.error_message}")
            try:
                db.execute(text("""
                    UPDATE twam."Orders" SET "Notes" = CONCAT(COALESCE("Notes", ''), :note),
                    "ModifiedDate" = :now WHERE "OrderId" = :oid
                """), {"note": f"[Ekart FAILED: {(result.error_message or '')[:200]}] ",
                       "oid": order_id, "now": datetime.now(timezone.utc)})
                db.commit()
            except Exception:
                db.rollback()
            try:
                _send_ekart_failure_alert(db, order, items, result.error_message or "Unknown error")
            except Exception as _aexc:
                print(f"[Payment→Ekart] STEP 8 ⚠️  Failure alert email failed: {_aexc}")
            return

        print(f"[Payment→Ekart] STEP 8 ✅ AWB={result.awb_number!r}")

        db.execute(text("""
            UPDATE twam."Orders" SET "TrackingId" = :tid, "State" = 'Processing',
            "IsShipped" = true, "DeliveryAgent" = 'EKart', "ModifiedDate" = :now
            WHERE "OrderId" = :oid
        """), {"tid": result.awb_number, "oid": order_id, "now": datetime.now(timezone.utc)})
        db.add(OrderTrackingStatus(orderId=order_id, status="Processing",
                                   createdDate=datetime.now(timezone.utc)))
        db.commit()
        print(f"[Payment→Ekart] STEP 9 ✅ DB updated → Processing | AWB={result.awb_number!r}")

        try:
            from app.shipment.models import Shipment
            qty = sum([i.quantity for i in items])
            ship = Shipment(
                orderId             = order_id,
                orderNumber         = order["order_number"],
                consigneeName       = order["name"],
                paymentMode         = order["payment_mode"],
                totalAmount         = order["total_amount"],
                taxValue            = 0,
                taxableAmount       = order["total_amount"],
                codAmount           = order["total_amount"] if order["payment_mode"] == "COD" else 0,
                quantity            = qty,
                weight              = 0.5,
                length              = 20,
                height              = 10,
                width               = 15,
                dropAddressId       = order["shipping_address_id"],
                isSameReturnAddress = True,
                trackingId          = result.awb_number,
                createdDate         = datetime.now(timezone.utc),
                deletedInd          = False,
            )
            db.add(ship)
            db.commit()
            print(f"[Payment→Ekart] STEP 9a ✅ Shipment table record created")
        except Exception as _e:
            db.rollback()
            print(f"[Payment→Ekart] STEP 9a ⚠️ Failed to insert shipment record: {_e}")

        try:
            from app.productvariantdetail.repository import increment_processed_quantity
            _now = datetime.now(timezone.utc)
            pvd_rows = db.execute(text("""
                SELECT "ProductVariantDetailId", SUM("Quantity")::INTEGER AS ordered_qty
                FROM twam."OrderItems" WHERE "OrderId" = :oid AND "DeletedInd" = false
                AND "ProductVariantDetailId" IS NOT NULL GROUP BY "ProductVariantDetailId"
            """), {"oid": order_id}).fetchall()
            updated_count = sum(1 for pvd_id, qty in pvd_rows if increment_processed_quantity(db, pvd_id, qty, now=_now))
            db.commit()
            print(f"[Payment→Ekart] STEP 9b ✅ {updated_count} variant(s) updated")
        except Exception as _se:
            db.rollback()
            print(f"[Payment→Ekart] STEP 9b ⚠️  Stock update failed (non-fatal): {_se}")

        try:
            send_order_shipped_notification(db, order_id)
            print(f"[Payment→Ekart] STEP 10 ✅ Notification sent")
        except Exception as _nex:
            print(f"[Payment→Ekart] STEP 10 ⚠️  Notification failed (non-fatal): {_nex}")

        print(f"[Payment→Ekart] ✅✅✅ COMPLETE — order {order_id} | AWB={result.awb_number!r}")
        print(f"{SEP}\n")

    except Exception as exc:
        import traceback
        print(f"[Payment→Ekart] ❌ UNEXPECTED ERROR for order {order_id}: {exc}")
        traceback.print_exc()
    finally:
        db.close()


# ── Background payment poller ──────────────────────────────────────────────
# Polls every 10s for 30 minutes. Hard-deletes order if PayG never responds.
# FIX: skips Detail API call if /Status endpoint checked within last 12s
#      to prevent duplicate simultaneous calls to PayG.

async def _poll_payment_until_resolved(order_id: int, order_key_id: str, unique_request_id: str) -> None:
    import asyncio
    from database import SessionLocal

    INITIAL_DELAY = 30
    POLL_INTERVAL = 10
    MAX_DURATION  = 1800  # 30 min
    MAX_POLLS     = MAX_DURATION // POLL_INTERVAL  # 180

    print(f"[PaymentPoller] ▶ START for order={order_id} — waiting {INITIAL_DELAY}s before first poll")
    await asyncio.sleep(INITIAL_DELAY)

    for attempt in range(1, MAX_POLLS + 1):
        await asyncio.sleep(POLL_INTERVAL)

        db = SessionLocal()
        try:
            order = db.query(Orders).filter(
                Orders.orderId == order_id, Orders.deletedInd == False,
            ).first()

            if not order:
                print(f"[PaymentPoller] Order {order_id} not found — stopping"); return

            current_state = (order.state or "").strip()
            RESOLVED = {"Confirmed", "Processing", "Shipped", "Delivered",
                        "PaymentFailed", "Cancelled", "Payment Failed"}
            if current_state in RESOLVED:
                print(f"[PaymentPoller] ✅ Order {order_id} already '{current_state}' — stopping"); return

            # Skip if /Status endpoint already checked PayG recently (within 12s)
            # This prevents the poller and /Status from hitting PayG simultaneously.
            last_modified = order.modifiedDate
            if last_modified:
                lm_aware = last_modified.replace(tzinfo=timezone.utc) if last_modified.tzinfo is None else last_modified
                seconds_since = (datetime.now(timezone.utc) - lm_aware).total_seconds()
                if seconds_since < 12:
                    print(f"[PaymentPoller] attempt={attempt}/{MAX_POLLS} | order={order_id} | skipping — /Status checked {seconds_since:.1f}s ago")
                    continue

            enriched = _enrich_params_via_detail_api(
                oid=str(order_id),
                rid=unique_request_id or order.paymentTransactionRefNo or "",
                order_key_id=order_key_id or order.orderKeyId or "",
            )

            response_code = int(enriched.get("PaymentResponseCode", -1)) if enriched else -1
            print(f"[PaymentPoller] attempt={attempt}/{MAX_POLLS} | order={order_id} | code={response_code} | state={current_state!r}")

            if response_code == 1:
                _update_order_after_payment(db, order, response_code, enriched)
                _record_payment_log(db, order_id, enriched.get("OrderKeyId", ""), enriched.get("UniqueRequestId", ""), enriched)
                db.commit()
                print(f"[PaymentPoller] ✅ Order {order_id} → Confirmed (attempt {attempt})")
                _clear_ordered_cart_items(db, order_id, order.userProfileId or "")
                _mark_brand_coupon_used_if_applicable(db, order_id)
                _send_payment_notification(db, order_id, success=True)
                await _auto_create_ekart_shipment(order_id)
                return

            elif response_code == 2:
                _update_order_after_payment(db, order, response_code, enriched)
                _record_payment_log(db, order_id, enriched.get("OrderKeyId", ""), enriched.get("UniqueRequestId", ""), enriched)
                db.execute(text("""
                    UPDATE twam."OrderItems" SET "DeletedInd" = true, "ModifiedDate" = :now
                    WHERE "OrderId" = :oid AND "DeletedInd" = false
                """), {"oid": order_id, "now": datetime.now(timezone.utc)})
                db.commit()
                print(f"[PaymentPoller] ❌ Order {order_id} → PaymentFailed (attempt {attempt})")
                _send_payment_notification(db, order_id, success=False)
                return

        except Exception as exc:
            print(f"[PaymentPoller] ⚠️  attempt={attempt} error for order {order_id}: {exc}")
        finally:
            db.close()

    # 30 min elapsed — hard delete if still unresolved
    print(f"[PaymentPoller] ⏰ 30 min elapsed for order {order_id} — deleting")
    db = SessionLocal()
    try:
        order = db.query(Orders).filter(Orders.orderId == order_id, Orders.deletedInd == False).first()
        if order and order.state in {"AwaitingPayment", "Pending", "Payment Pending"}:
            db.execute(text('DELETE FROM twam."OrderItems" WHERE "OrderId" = :oid'), {"oid": order_id})
            db.execute(text('DELETE FROM twam."OrderTrackingStatus" WHERE "OrderId" = :oid'), {"oid": order_id})
            db.execute(text('DELETE FROM twam."Orders" WHERE "OrderId" = :oid'), {"oid": order_id})
            db.commit()
            print(f"[PaymentPoller] ✅ Order {order_id} hard-deleted after 30 min")
        else:
            print(f"[PaymentPoller] Order {order_id} state='{getattr(order,'state','N/A')}' — already resolved")
    except Exception as exc:
        db.rollback()
        print(f"[PaymentPoller] ❌ Timeout deletion failed for order {order_id}: {exc}")
    finally:
        db.close()


# ── Callback ───────────────────────────────────────────────────────────────

@router.get("/Callback",  include_in_schema=True)
@router.post("/Callback", include_in_schema=True)
async def payment_callback(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    SEP = "=" * 60
    params: dict = {}
    print(f"[Payment:Callback] frontend_base_url raw = {get_setting('frontend_base_url', 'NOT_FOUND')}")
    print(f"[Payment:Callback] _frontend_url() = {_frontend_url()}")
    params.update(dict(request.query_params))

    if request.method == "POST":
        ct = request.headers.get("content-type", "")
        if "application/json" in ct:
            try:
                body_params = await request.json()
                if isinstance(body_params, dict): params.update(body_params)
            except Exception: pass
        else:
            try:
                form = await request.form()
                params.update(dict(form))
            except Exception: pass

    print(f"\n{SEP}")
    print(f"[Payment:Callback] ▶ STEP 1 — Received {request.method} callback from PayG")
    print(f"[Payment:Callback]   URL={request.url}")
    print(f"[Payment:Callback]   Raw params={json.dumps(params, default=str)}")

    oid = params.get("oid", "")
    rid = params.get("rid", "")
    payg_response_code_raw = params.get("PaymentResponseCode")
    try:
        payg_response_code_direct = int(payg_response_code_raw) if payg_response_code_raw not in (None, "", "-1") else None
    except (ValueError, TypeError):
        payg_response_code_direct = None
    payg_gave_us_params = payg_response_code_direct in (1, 2)

    print(f"[Payment:Callback] STEP 1 ✅ Parsed | oid={oid!r} | rid={rid!r} | payg_gave_us_params={payg_gave_us_params} | PaymentResponseCode={payg_response_code_raw!r}")

    order_key_id_from_cb = params.get("OrderKeyId", "")
    if not order_key_id_from_cb and oid:
        try:
            _early_order = db.query(Orders).filter(Orders.orderId == int(oid), Orders.deletedInd == False).first()
            if _early_order and _early_order.orderKeyId:
                order_key_id_from_cb = _early_order.orderKeyId
                print(f"[Payment:Callback] STEP 2 — Resolved OrderKeyId={order_key_id_from_cb!r} from DB")
        except Exception as _e:
            print(f"[Payment:Callback] STEP 2 — Could not pre-fetch orderKeyId: {_e}")

    if not payg_gave_us_params and (oid or rid or order_key_id_from_cb):
        print(f"[Payment:Callback] STEP 2 — Calling PayG Detail API...")
        enriched = _enrich_params_via_detail_api(oid, rid, order_key_id=order_key_id_from_cb)
        if enriched and enriched.get("PaymentResponseCode") not in (None, "", -1):
            params = enriched
            print(f"[Payment:Callback] STEP 2 ✅ Detail API resolved | code={params.get('PaymentResponseCode')!r}")
        else:
            if payg_response_code_direct is not None:
                print(f"[Payment:Callback] STEP 2 ⚠️  Detail API failed but PayG gave code={payg_response_code_direct} directly")
            else:
                print(f"[Payment:Callback] STEP 2 ❌ Detail API failed AND no code from PayG — code=-1")
    elif payg_gave_us_params:
        print(f"[Payment:Callback] STEP 2 — Skipped Detail API — PayG gave code={payg_response_code_raw!r} directly")
    else:
        print(f"[Payment:Callback] STEP 2 ❌ Cannot call Detail API — no oid/rid/OrderKeyId")

    order_id_str  = params.get("UserDefined1") or oid or ""
    order_key_id  = params.get("OrderKeyId", "")
    unique_req_id = params.get("UniqueRequestId", "") or rid

    print(f"[Payment:Callback] STEP 3 — Looking up order | order_id_str={order_id_str!r}")
    order: Optional[Orders] = None
    if order_id_str:
        try:
            order = db.query(Orders).filter(Orders.orderId == int(order_id_str), Orders.deletedInd == False).first()
            print(f"[Payment:Callback] STEP 3 — Lookup by orderId: {'FOUND' if order else 'NOT FOUND'}")
        except Exception as e:
            print(f"[Payment:Callback] STEP 3 ⚠️  orderId lookup error: {e}")
    if order is None and order_key_id:
        order = db.query(Orders).filter(Orders.orderKeyId == order_key_id, Orders.deletedInd == False).first()
        print(f"[Payment:Callback] STEP 3 — Lookup by orderKeyId: {'FOUND' if order else 'NOT FOUND'}")
    if order is None and unique_req_id:
        order = db.query(Orders).filter(Orders.paymentTransactionRefNo == unique_req_id, Orders.deletedInd == False).first()
        print(f"[Payment:Callback] STEP 3 — Lookup by uniqueRequestId: {'FOUND' if order else 'NOT FOUND'}")

    if order is None:
        print(f"[Payment:Callback] STEP 3 ❌ ORDER NOT FOUND")
        return RedirectResponse(url=f"{_frontend_url()}/user-products/payment-callback?status=error&message=Order+not+found", status_code=302)

    order_id = order.orderId
    print(f"[Payment:Callback] STEP 3 ✅ Order found | id={order_id} | number={order.orderNumber!r} | state={order.state!r}")

    try:
        response_code = int(params.get("PaymentResponseCode", -1))
    except (ValueError, TypeError):
        response_code = -1

    success = (response_code == 1)
    new_state = payg_client.payment_code_to_order_state(response_code)
    print(f"[Payment:Callback] STEP 5 — outcome | code={response_code} | success={success} | new_state={new_state!r}")

    if response_code == -1:
        print(f"[Payment:Callback] STEP 6 ℹ️  code=-1 — skipping state update")
    else:
        try:
            _update_order_after_payment(db, order, response_code, params)
            _record_payment_log(db, order_id, order_key_id, unique_req_id, params)
            db.commit()
            print(f"[Payment:Callback] STEP 6 ✅ DB updated → {new_state!r}")
        except Exception as exc:
            db.rollback()
            print(f"[Payment:Callback] STEP 6 ❌ DB update FAILED: {exc}")

    if success:
        print(f"[Payment:Callback] STEP 7 — SUCCESS ✅ | Queueing Ekart shipment...")
        background_tasks.add_task(_auto_create_ekart_shipment, order_id)
        _clear_ordered_cart_items(db, order_id)
        _mark_brand_coupon_used_if_applicable(db, order_id)
    elif response_code == 2:
        try:
            cleanup = db.execute(text("""
                UPDATE twam."OrderItems" SET "DeletedInd" = true, "ModifiedDate" = :now
                WHERE "OrderId" = :oid AND "DeletedInd" = false
            """), {"oid": order_id, "now": datetime.now(timezone.utc)})
            db.commit()
            print(f"[Payment:Callback] STEP 7 ✅ Soft-deleted {cleanup.rowcount} OrderItem(s)")
        except Exception as exc:
            db.rollback()
    else:
        print(f"[Payment:Callback] STEP 7 — AMBIGUOUS (code={response_code}) — preserving order")

    if response_code == 1:
        _send_payment_notification(db, order_id, success=True)
        print(f"[Payment:Callback] STEP 8 ✅ Success notification sent")
    elif response_code == 2:
        _send_payment_notification(db, order_id, success=False)
        print(f"[Payment:Callback] STEP 8 ✅ Failure notification sent")
    else:
        print(f"[Payment:Callback] STEP 8 ℹ️  code={response_code} — skipping notification")

    if success:
        redirect_url = f"{_frontend_url()}/user-products/order-confirm/{order_id}?payment=success"
    elif response_code == 2:
        reason = params.get("PaymentReasonCode") or "declined"
        redirect_url = f"{_frontend_url()}/user-products/payment-callback?status=failed&orderId={order_id}&code={response_code}&reason={reason}"
    else:
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


@router.post("/Webhook", include_in_schema=True)
async def payment_webhook(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    SEP = "=" * 60
    print(f"\n{SEP}\n[Payment:Webhook] ▶ Received S2S webhook from PayG")
    params: dict = {}
    params.update(dict(request.query_params))
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        try:
            body = await request.json()
            if isinstance(body, dict): params.update(body)
        except Exception: pass
    else:
        try:
            form = await request.form()
            params.update(dict(form))
        except Exception: pass

    print(f"[Payment:Webhook] Raw params={json.dumps(params, default=str)}")
    try: response_code = int(params.get("PaymentResponseCode", -1))
    except (ValueError, TypeError): response_code = -1

    order_id_str  = params.get("UserDefined1", "") or params.get("oid", "")
    order_key_id  = params.get("OrderKeyId", "")
    unique_req_id = params.get("UniqueRequestId", "")

    order = None
    if order_id_str:
        try: order = db.query(Orders).filter(Orders.orderId == int(order_id_str), Orders.deletedInd == False).first()
        except (ValueError, TypeError): pass
    if order is None and order_key_id:
        order = db.query(Orders).filter(Orders.orderKeyId == order_key_id, Orders.deletedInd == False).first()
    if order is None and unique_req_id:
        order = db.query(Orders).filter(Orders.paymentTransactionRefNo == unique_req_id, Orders.deletedInd == False).first()
    if order is None:
        print(f"[Payment:Webhook] ❌ Order not found")
        return {"status": "order_not_found"}

    order_id = order.orderId
    was_confirmed = (order.state == "Confirmed")
    new_state = payg_client.payment_code_to_order_state(response_code)
    success = (response_code == 1)
    print(f"[Payment:Webhook] order={order_id} | code={response_code} | new_state={new_state!r}")

    if response_code in (1, 2) and not was_confirmed:
        try:
            _update_order_after_payment(db, order, response_code, params)
            _record_payment_log(db, order_id, order_key_id, unique_req_id, params)
            db.commit()
        except Exception as exc:
            db.rollback()
            print(f"[Payment:Webhook] ❌ DB update failed: {exc}")

    if success and not was_confirmed:
        background_tasks.add_task(_auto_create_ekart_shipment, order_id)
        _clear_ordered_cart_items(db, order_id)
        _mark_brand_coupon_used_if_applicable(db, order_id)
        _send_payment_notification(db, order_id, success=True)
    elif response_code == 2 and not was_confirmed:
        _send_payment_notification(db, order_id, success=False)

    print(f"{SEP}\n")
    return {"status": "ok", "orderId": order_id, "state": new_state}


@router.post("/AdminConfirm/{order_id}", include_in_schema=True)
def admin_confirm_payment(order_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db), current_user: CurrentUser = Depends(get_current_user)):
    if current_user.role_id not in Roles.STAFF:
        raise HTTPException(403, "Staff only")
    order = db.query(Orders).filter(Orders.orderId == order_id, Orders.deletedInd == False).first()
    if not order: raise HTTPException(404, "Order not found")
    SAFE_STATES = {"AwaitingPayment", "Pending", "Payment Pending"}
    if order.state not in SAFE_STATES:
        return {"success": False, "message": f"Order already in state '{order.state}'", "state": order.state}
    order.state = "Confirmed"
    order.modifiedDate = datetime.now(timezone.utc)
    db.add(order)
    db.commit()
    background_tasks.add_task(_auto_create_ekart_shipment, order_id)
    _clear_ordered_cart_items(db, order_id)
    _send_payment_notification(db, order_id, success=True)
    print(f"[Payment:AdminConfirm] ✅ Order {order_id} manually confirmed by staff {current_user.user_id}")
    return {"success": True, "orderId": order_id, "state": "Confirmed"}


@router.get("/Debug/{order_id}")
def debug_payment(order_id: int, db: Session = Depends(get_db), current_user: CurrentUser = Depends(get_current_user)):
    if current_user.role_id not in Roles.STAFF: raise HTTPException(403, "Admin only")
    order = db.query(Orders).filter(Orders.orderId == order_id, Orders.deletedInd == False).first()
    if not order: return {"order_found": False, "diagnosis": f"Order {order_id} not in this DB."}
    payg_detail, payg_error = {}, ""
    if order.paymentTransactionRefNo or order.orderKeyId:
        try:
            payg_detail = payg_client.get_payg_order_detail(order_key_id=order.orderKeyId or None, unique_request_id=order.paymentTransactionRefNo or None)
        except Exception as e:
            payg_error = str(e)
    from app.ekart.client import load_ekart_config_from_db
    cfg = load_ekart_config_from_db(db)
    tracking_val = getattr(order, "trackingId", None) or getattr(order, "TrackingId", None)
    return {
        "order_found": True,
        "order": {"id": order.orderId, "number": order.orderNumber, "state": order.state,
                  "tracking_id": tracking_val, "payment_method": order.paymentMethod,
                  "ref_no": order.paymentTransactionRefNo, "order_key_id": order.orderKeyId},
        "payg_live": {"response_code": payg_detail.get("PaymentResponseCode"),
                      "order_status": payg_detail.get("OrderStatus"),
                      "amount": payg_detail.get("OrderAmount"), "error": payg_error or None},
        "ekart": {"config_found": cfg is not None, "config_complete": cfg.is_complete if cfg else False},
        "settings": {"payg_mode": get_setting("payg_mode"), "payg_redirect_url": get_setting("payg_redirect_url"),
                     "frontend_base_url": get_setting("frontend_base_url")},
    }


@router.get("/Status/{order_id}")
async def get_payment_status(
    order_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: Optional[CurrentUser] = Depends(get_current_user_optional),
):
    order = db.query(Orders).filter(Orders.orderId == order_id, Orders.deletedInd == False).first()
    if not order: raise HTTPException(404, "Order not found")
    if current_user and current_user.role_id not in Roles.STAFF:
        if order.userProfileId != current_user.user_id: raise HTTPException(403, "Not authorised")

    local_state = order.state or "AwaitingPayment"
    UNRESOLVED  = {"AwaitingPayment", "Pending", "Payment Pending", "awaitingpayment"}

    if local_state in UNRESOLVED:
        # Skip if background poller already checked PayG within last 12s
        # Prevents duplicate simultaneous Detail API calls from mobile polling + poller
        skip_probe = False
        last_modified = order.modifiedDate
        if last_modified:
            lm_aware = last_modified.replace(tzinfo=timezone.utc) if last_modified.tzinfo is None else last_modified
            seconds_ago = (datetime.now(timezone.utc) - lm_aware).total_seconds()
            if seconds_ago < 12:
                skip_probe = True
                print(f"[Payment/Status] Order {order_id} — skipping probe (poller checked {seconds_ago:.1f}s ago)")

        if not skip_probe:
            print(f"[Payment/Status] Order {order_id} is '{local_state}' — probing PayG Detail API...")
            try:
                enriched = _enrich_params_via_detail_api(
                    oid=str(order_id),
                    rid=order.paymentTransactionRefNo or "",
                    order_key_id=order.orderKeyId or "",
                )
                response_code = int(enriched.get("PaymentResponseCode", -1)) if enriched else -1
                print(f"[Payment/Status] PayG Detail API returned code={response_code} for order {order_id}")

                if response_code == 1:
                    _update_order_after_payment(db, order, response_code, enriched)
                    _record_payment_log(db, order_id, enriched.get("OrderKeyId", ""), enriched.get("UniqueRequestId", ""), enriched)
                    db.commit()
                    local_state = "Confirmed"
                    print(f"[Payment/Status] ✅ Order {order_id} → Confirmed")
                    _clear_ordered_cart_items(db, order_id, order.userProfileId or "")
                    _mark_brand_coupon_used_if_applicable(db, order_id)
                    background_tasks.add_task(_auto_create_ekart_shipment, order_id)
                    _send_payment_notification(db, order_id, success=True)
                elif response_code == 2:
                    _update_order_after_payment(db, order, response_code, enriched)
                    _record_payment_log(db, order_id, enriched.get("OrderKeyId", ""), enriched.get("UniqueRequestId", ""), enriched)
                    db.execute(text("""
                        UPDATE twam."OrderItems" SET "DeletedInd" = true, "ModifiedDate" = :now
                        WHERE "OrderId" = :oid AND "DeletedInd" = false
                    """), {"oid": order_id, "now": datetime.now(timezone.utc)})
                    db.commit()
                    local_state = "PaymentFailed"
                    print(f"[Payment/Status] ❌ Order {order_id} → PaymentFailed")
                    _send_payment_notification(db, order_id, success=False)
                else:
                    print(f"[Payment/Status] ⏳ Order {order_id} still pending (code={response_code}) — retry")
            except Exception as exc:
                print(f"[Payment/Status] ⚠️  Detail API probe failed for order {order_id}: {exc}")

    elif local_state == "Confirmed":
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


@router.post("/Expire/{order_id}")
def expire_payment(order_id: int, db: Session = Depends(get_db), current_user: CurrentUser = Depends(get_current_user)):
    order = db.query(Orders).filter(Orders.orderId == order_id, Orders.deletedInd == False).first()
    if not order: raise HTTPException(404, "Order not found")
    if current_user.role_id not in Roles.STAFF and order.userProfileId != current_user.user_id:
        raise HTTPException(403, "Not authorised")
    SAFE_TO_EXPIRE = {"AwaitingPayment", "Pending", "Payment Pending"}
    if order.state not in SAFE_TO_EXPIRE:
        return {"success": False, "state": order.state, "message": "Order already resolved"}
    order.state = "PaymentFailed"
    order.modifiedDate = datetime.now(timezone.utc)
    db.add(order)
    db.add(OrderTrackingStatus(orderId=order_id, status="PaymentFailed", createdDate=datetime.now(timezone.utc)))
    db.commit()
    try:
        result = db.execute(text("""
            UPDATE twam."OrderItems" SET "DeletedInd" = true, "ModifiedDate" = :now
            WHERE "OrderId" = :oid AND "DeletedInd" = false
        """), {"oid": order_id, "now": datetime.now(timezone.utc)})
        db.commit()
        print(f"[Payment/Expire] Order {order_id} expired → PaymentFailed | {result.rowcount} items soft-deleted")
    except Exception as exc:
        db.rollback()
    return {"success": True, "state": "PaymentFailed"}


class PaygConfigResponse(BaseModel):
    payg_mode: str; payg_mid: str; payg_auth_key: str; payg_auth_token: str
    payg_secure_hash: str; payg_merchant_key_id: str; payg_redirect_url: str; frontend_base_url: str

class PaygConfigUpdate(BaseModel):
    payg_mode: Optional[str] = None; payg_mid: Optional[str] = None
    payg_auth_key: Optional[str] = None; payg_auth_token: Optional[str] = None
    payg_secure_hash: Optional[str] = None; payg_merchant_key_id: Optional[str] = None
    payg_redirect_url: Optional[str] = None; frontend_base_url: Optional[str] = None

def _mask(v: str) -> str:
    if not v or len(v) <= 8: return "****"
    return v[:4] + "****" + v[-4:]

@router.get("/OutboundIp")
def get_outbound_ip():
    import httpx as _httpx
    results = {}
    for svc, url in [("amazonaws", "https://checkip.amazonaws.com"), ("ipify", "https://api.ipify.org")]:
        try: r = _httpx.get(url, timeout=5); results[svc] = r.text.strip(); break
        except Exception as e: results[svc] = f"failed: {e}"
    return {"outbound_ip": results}

@router.get("/Config", response_model=PaygConfigResponse, dependencies=[Depends(require_roles(Roles.SUPER_ADMIN))])
def get_payg_config():
    return PaygConfigResponse(
        payg_mode=get_setting("payg_mode", "uat"), payg_mid=get_setting("payg_mid", ""),
        payg_auth_key=_mask(get_setting("payg_auth_key", "")), payg_auth_token=_mask(get_setting("payg_auth_token", "")),
        payg_secure_hash=_mask(get_setting("payg_secure_hash", "")), payg_merchant_key_id=get_setting("payg_merchant_key_id", ""),
        payg_redirect_url=get_setting("payg_redirect_url", ""), frontend_base_url=get_setting("frontend_base_url"),
    )

@router.put("/Config", dependencies=[Depends(require_roles(Roles.SUPER_ADMIN))])
def update_payg_config(data: PaygConfigUpdate, db: Session = Depends(get_db)):
    from app.common.models import AppSettings
    updates = {k: v for k, v in data.model_dump().items() if v is not None}
    now = datetime.now(timezone.utc)
    for key, value in updates.items():
        row = db.query(AppSettings).filter(AppSettings.key == key, AppSettings.deletedInd == False).first()
        if row: row.value = value; row.modifiedDate = now
        else: db.add(AppSettings(key=key, value=value, description=f"PayG: {key}", isActive=True, createdDate=now, modifiedDate=now, deletedInd=False))
    db.commit()
    reload_settings(db)
    return {"success": True, "updated": list(updates.keys())}
