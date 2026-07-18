"""
app/ekart/router.py  — Ekart Elite Logistics Router
─────────────────────────────────────────────────────
All Ekart API features exposed as REST endpoints:
  POST   /api/Ekart/CreateShipment          — single forward shipment
  POST   /api/Ekart/BulkShipment            — bulk forward shipments (multiple orders)
  POST   /api/Ekart/CreateReturnShipment    — reverse/return shipment
  DELETE /api/Ekart/CancelShipment          — cancel shipment on Ekart
  GET    /api/Ekart/Track/{tracking_id}     — live tracking
  POST   /api/Ekart/UpdateStatus            — manual order status update
  POST   /api/Ekart/DownloadLabel           — download shipping label PDF
  POST   /api/Ekart/DownloadManifest        — download manifest PDF
  POST   /api/Ekart/NDRAction               — Non-Delivery Report action
  GET    /api/Ekart/Serviceability/{pincode}— serviceability check v2
  POST   /api/Ekart/ServiceabilityV3        — serviceability check v3
  POST   /api/Ekart/ShippingEstimate        — shipping rate calculator
  POST   /api/Ekart/SetDispatchDate         — set preferred dispatch date
  GET    /api/Ekart/CODReport               — COD cash collection report
  GET    /api/Ekart/Config                  — view ShipmentConfigurations
  PUT    /api/Ekart/Config/{id}             — update ShipmentConfigurations
  GET    /api/Ekart/Readiness               — check live-readiness

FIXES APPLIED:
  1. _get_order_items_for_shipment: p."SKU" → COALESCE(pv."SKU", ProductId)
     p."SKU" does not exist on twam.Products — SKU is on ProductVariants.
     This caused admin manual shipment creation to crash with a DB column error.
  2. _build_return_address_from_db: stateId == "Active" → case-insensitive + NULL-safe
     Same bug as in client.py — exact string match fails when value is NULL or wrong case.
  3. ekart_readiness endpoint: same stateId fix so Readiness check returns correct status.
"""

from datetime import datetime, timezone
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Response
from sqlalchemy.orm import Session
from sqlalchemy import text, or_, func as sa_func
from pydantic import BaseModel

from database import get_db
from app.auth.dependencies import get_current_user, get_current_admin_user
from app.orders.models import Orders, OrderTrackingStatus
from app.ekart.client import get_ekart_client_for_db
from app.ekart.schemas import (
    CreateShipmentRequest,
    CreateReturnShipmentRequest,
    EkartAddress,
    EkartShipmentItem,
    CancelShipmentRequest,
)
from app.ekart.notifications import (
    send_order_shipped_notification,
    send_order_cancelled_notification,
    send_return_initiated_notification,
    send_order_status_notification,
)

router = APIRouter(prefix="/api/Ekart", tags=["Ekart Logistics"])


# ── Request / Response Models ─────────────────────────────────────────────────

class CreateShipmentInput(BaseModel):
    order_id: int
    manual_tracking_id: Optional[str] = None
    delivery_days: Optional[int] = None


class BulkShipmentInput(BaseModel):
    order_ids: List[int]


class CreateReturnInput(BaseModel):
    order_id: int
    return_reason: str


class UpdateOrderStatusInput(BaseModel):
    order_id: int
    status: str
    tracking_id: Optional[str] = None
    remarks: Optional[str] = None


class CancelShipmentInput(BaseModel):
    order_id: int
    reason: Optional[str] = "Customer requested cancellation"


class LabelDownloadInput(BaseModel):
    awb_numbers: List[str]
    json_only: bool = False


class ManifestInput(BaseModel):
    awb_numbers: List[str]


class NDRActionInput(BaseModel):
    tracking_id: str
    action: str  # "RE_ATTEMPT" | "RTO" | "CONFIRM_DELIVERY"
    remarks: Optional[str] = ""
    preferred_date: Optional[str] = None  # "YYYY-MM-DD"


class ServiceabilityV3Input(BaseModel):
    pickup_pincode: str
    drop_pincode: str
    weight: float = 0.5
    length: float = 30
    width: float = 25
    height: float = 5


class ShippingEstimateInput(BaseModel):
    pickup_pincode: str
    drop_pincode: str
    weight: float = 0.5
    payment_mode: str = "Prepaid"  # "Prepaid" | "COD"
    cod_amount: float = 0


class DispatchDateInput(BaseModel):
    awb_numbers: List[str]
    dispatch_date: str  # "YYYY-MM-DD"


class ShipmentConfigUpdate(BaseModel):
    clientId: Optional[str] = None
    accessTokenURL: Optional[str] = None
    createShipmentURL: Optional[str] = None
    cancelShipmentURL: Optional[str] = None
    trackShipmentURL: Optional[str] = None
    wayBillURL: Optional[str] = None
    userName: Optional[str] = None
    password: Optional[str] = None
    returnName: Optional[str] = None
    returnPhone: Optional[str] = None
    returnAddressLine1: Optional[str] = None
    returnAddressLine2: Optional[str] = None
    returnCity: Optional[str] = None
    returnState: Optional[str] = None
    returnPinCode: Optional[str] = None
    returnCountry: Optional[str] = None
    # Set after calling /api/Ekart/RegisterAddress — enables alias-only pickup mode
    pickupAlias: Optional[str] = None


class ShipmentResponse(BaseModel):
    success: bool
    order_id: Optional[int] = None
    tracking_id: Optional[str] = None
    tracking_url: Optional[str] = None
    message: Optional[str] = None
    error: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_order_with_address(db: Session, order_id: int) -> Optional[dict]:
    print(f"[Ekart:_get_order_with_address] 🔍 Fetching order {order_id} from DB...")
    try:
        row = db.execute(text("""
            SELECT
                o."OrderId", o."OrderNumber", o."TotalAmount", o."State",
                o."TrackingId", o."PaymentMethod", o."DeliveryCharge",
                a."Name", a."Phone", a."AddressLine", a."City",
                a."PinCode", s."StateName", c."CountryName",
                p."EmailId",
                o."ShippingTypeId", st."ShippingTypeName"
            FROM twam."Orders" o
            LEFT JOIN twam."Address" a ON a."AddressId" = o."ShippingAddressId"
            LEFT JOIN mdm."State" s ON s."StateId" = a."StateId"
            LEFT JOIN mdm."Country" c ON c."CountryId" = a."CountryId"
            LEFT JOIN twam."People" p ON p."UserProfileId" = o."UserProfileId"
            LEFT JOIN mdm."ShippingType" st ON st."ShippingTypeId" = o."ShippingTypeId"
                AND st."DeletedInd" = false
            WHERE o."OrderId" = :order_id AND o."DeletedInd" = false
        """), {"order_id": order_id}).fetchone()
    except Exception as db_exc:
        print(f"[Ekart:_get_order_with_address] ❌ DB query FAILED for order {order_id}: {db_exc}")
        raise

    if not row:
        print(f"[Ekart:_get_order_with_address] ❌ Order {order_id} NOT FOUND in DB (deleted or wrong id)")
        return None

    result = {
        "order_id":           row[0],
        "order_number":       row[1] or str(row[0]),
        "total_amount":       float(row[2]) if row[2] else 0,
        "state":              row[3],
        "tracking_id":        row[4],
        "payment_mode":       row[5] or "PREPAID",
        "delivery_charge":    float(row[6]) if row[6] else 0,
        "name":               row[7],
        "phone":              row[8],
        "address_line":       row[9],
        "city":               row[10],
        "pincode":            row[11],
        "state_name":         row[12] or "",
        "country":            row[13] or "India",
        "email":              row[14],
        "shipping_type_id":   row[15],
        "shipping_type_name": row[16] or "Normal",
    }

    print(
        f"[Ekart:_get_order_with_address] ✅ Found order {order_id} | "
        f"state={result['state']} | tracking={result['tracking_id']!r} | "
        f"city={result['city']} | pin={result['pincode']} | "
        f"total=₹{result['total_amount']} | payment_mode={result['payment_mode']} | "
        f"address_line={result['address_line']!r} | state_name={result['state_name']!r} | "
        f"name={result['name']!r} | phone={result['phone']!r} | "
        f"shipping_type={result['shipping_type_name']!r}"
    )

    # Warn about missing address fields that would cause Ekart API rejection
    missing = []
    if not result["name"]:         missing.append("consignee name")
    if not result["phone"]:        missing.append("phone")
    if not result["address_line"]: missing.append("address_line")
    if not result["city"]:         missing.append("city")
    if not result["pincode"]:      missing.append("pincode")
    if not result["state_name"]:   missing.append("state_name (StateId may not be set on Address)")
    if missing:
        print(f"[Ekart:_get_order_with_address] ⚠️  MISSING FIELDS for order {order_id}: {', '.join(missing)}")

    return result


def _get_order_items_for_shipment(db: Session, order_id: int) -> list:
    # TaxHSNCodeId is on twam.Products (product-level GST classification).
    # HSN does not change per colour/size — product level is the correct source.
    # TaxHSNCode lookup table is in mdm schema.
    print(f"[Ekart:_get_order_items_for_shipment] 🔍 Fetching items for order {order_id}...")
    try:
        rows = db.execute(text("""
            SELECT
                p."Name",
                COALESCE(pv."ProductCode", CAST(oi."ProductId" AS TEXT)) AS sku,
                oi."Quantity", oi."Price",
                thc."HSNCode"
            FROM twam."OrderItems" oi
            LEFT JOIN twam."Products" p         ON p."ProductId"         = oi."ProductId"
            LEFT JOIN twam."ProductVariants" pv ON pv."ProductVariantId" = oi."ProductVariantId"
            LEFT JOIN mdm."TaxHSNCode" thc      ON thc."TaxHSNCodeId"    = p."TaxHSNCodeId"
            WHERE oi."OrderId" = :order_id AND oi."DeletedInd" = false
        """), {"order_id": order_id}).fetchall()
    except Exception as db_exc:
        print(f"[Ekart:_get_order_items_for_shipment] ❌ DB query FAILED for order {order_id}: {db_exc}")
        raise

    if not rows:
        print(f"[Ekart:_get_order_items_for_shipment] ⚠️  NO items found for order {order_id} — shipment will be rejected by Ekart!")
        return []

    items = [
        EkartShipmentItem(
            name=r[0] or "Product",
            sku=r[1],
            quantity=r[2] or 1,
            price=float(r[3]) if r[3] else 0,
            hsn_code=r[4],
        )
        for r in rows
    ]
    print(f"[Ekart:_get_order_items_for_shipment] ✅ {len(items)} item(s) for order {order_id}: "
          + ", ".join(f"{i.name}(qty={i.quantity}, ₹{i.price})" for i in items))
    return items


# ── Admin: Failed Shipments list ───────────────────────────────────────────
#
# "Failed" = payment succeeded (order state is past AwaitingPayment, i.e.
# Confirmed/Processing/etc) but no TrackingId has been written yet — meaning
# the automatic _auto_create_ekart_shipment background task either failed
# or hasn't run. These need manual intervention by support staff.

@router.get("/FailedShipments")
def list_failed_shipments(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_admin_user),
):
    """
    Lists orders where payment succeeded but Ekart shipment creation has not
    completed (no TrackingId yet). Admin uses this page to either retry
    auto-creation or manually enter a tracking ID created via the Ekart portal.
    """
    # Use ORM query to avoid raw SQL column casing issues with TrackingId
    from app.orders.models import Orders as OrderModel
    order_rows = (
        db.query(OrderModel)
        .filter(
            OrderModel.deletedInd == False,
            (OrderModel.trackingId == None) | (OrderModel.trackingId == ""),
            OrderModel.state.notin_([
                "AwaitingPayment", "Pending", "Payment Pending",
                "PaymentFailed", "Cancelled",
            ]),
        )
        .order_by(OrderModel.createdDate.desc())
        .all()
    )

    # Now fetch address/state/email via a single raw query per order (or join)
    order_ids = [o.orderId for o in order_rows]
    if not order_ids:
        return {"count": 0, "failedShipments": []}

    placeholders = ", ".join(str(oid) for oid in order_ids)
    detail_rows = db.execute(text(f"""
        SELECT
            o."OrderId",
            a."Name", a."Phone", a."AddressLine", a."City",
            a."PinCode", s."StateName",
            p."EmailId",
            o."PaymentMethod", o."TotalAmount",
            o."State", o."CreatedDate"
        FROM twam."Orders" o
        LEFT JOIN twam."Address" a ON a."AddressId" = o."ShippingAddressId"
        LEFT JOIN mdm."State" s    ON s."StateId"   = a."StateId"
        LEFT JOIN twam."People" p  ON p."UserProfileId" = o."UserProfileId"
        WHERE o."OrderId" IN ({placeholders})
    """)).fetchall()

    detail_map = {r[0]: r for r in detail_rows}

    results = []
    for o in order_rows:
        r = detail_map.get(o.orderId)
        items = _get_order_items_for_shipment(db, o.orderId)
        results.append({
            "orderId":       o.orderId,
            "orderNumber":   o.orderNumber or str(o.orderId),
            "totalAmount":   float(r[9]) if r and r[9] else 0,
            "state":         o.state,
            "paymentMethod": r[8] if r else None,
            "createdDate":   o.createdDate,
            "failureNotes":  None,
            "consignee": {
                "name":    r[1] if r else None,
                "phone":   r[2] if r else None,
                "address": r[3] if r else None,
                "city":    r[4] if r else None,
                "pincode": r[5] if r else None,
                "state":   r[6] if r else "",
            },
            "email": r[7] if r else None,
            "items": [
                {"name": i.name, "sku": i.sku, "quantity": i.quantity,
                 "price": i.price, "hsnCode": i.hsn_code}
                for i in items
            ],
        })

    return {"count": len(results), "failedShipments": results}


@router.get("/ResolvedShipments")
def list_resolved_shipments(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_admin_user),
):
    """
    Orders that previously failed shipment creation but have since been resolved
    (TrackingId is now present and state is Processing/Shipped/Delivered).
    Shown in the Resolved tab of the Failed Shipments admin page.
    """
    from app.orders.models import Orders as OrderModel
    from app.shared.app_settings import get_setting as _gs
    tracking_base = _gs("TRACKING_BASE_URL", "https://app.elite.ekartlogistics.in/track") or "https://app.elite.ekartlogistics.in/track"

    order_rows = (
        db.query(OrderModel)
        .filter(
            OrderModel.deletedInd == False,
            OrderModel.trackingId != None,
            OrderModel.trackingId != "",
        )
        .order_by(OrderModel.modifiedDate.desc())
        .limit(100)
        .all()
    )

    # Filter to only those that had a Notes failure marker
    order_rows = [o for o in order_rows if o.notes and "Ekart" in (o.notes or "") and "FAILED" in (o.notes or "")]

    order_ids = [o.orderId for o in order_rows]
    if not order_ids:
        return {"count": 0, "resolvedShipments": []}

    placeholders = ", ".join(str(oid) for oid in order_ids)
    detail_rows = db.execute(text(f"""
        SELECT
            o."OrderId",
            a."Name", a."Phone", a."AddressLine", a."City",
            a."PinCode", s."StateName",
            p."EmailId",
            o."PaymentMethod", o."TotalAmount", o."State"
        FROM twam."Orders" o
        LEFT JOIN twam."Address" a ON a."AddressId" = o."ShippingAddressId"
        LEFT JOIN mdm."State" s    ON s."StateId"   = a."StateId"
        LEFT JOIN twam."People" p  ON p."UserProfileId" = o."UserProfileId"
        WHERE o."OrderId" IN ({placeholders})
    """)).fetchall()

    detail_map = {r[0]: r for r in detail_rows}

    results = []
    for o in order_rows:
        r = detail_map.get(o.orderId)
        tracking_id = o.trackingId
        items = _get_order_items_for_shipment(db, o.orderId)
        results.append({
            "orderId":       o.orderId,
            "orderNumber":   o.orderNumber or str(o.orderId),
            "totalAmount":   float(r[9]) if r and r[9] else 0,
            "state":         o.state,
            "paymentMethod": r[8] if r else None,
            "createdDate":   o.createdDate,
            "failureNotes":  None,
            "trackingId":    tracking_id,
            "trackingUrl":   f"{tracking_base.rstrip('/')}/{tracking_id}" if tracking_id else None,
            "consignee": {
                "name":    r[1] if r else None,
                "phone":   r[2] if r else None,
                "address": r[3] if r else None,
                "city":    r[4] if r else None,
                "pincode": r[5] if r else None,
                "state":   r[6] if r else "",
            },
            "email": r[7] if r else None,
            "items": [
                {"name": i.name, "sku": i.sku, "quantity": i.quantity,
                 "price": i.price, "hsnCode": i.hsn_code}
                for i in items
            ],
        })

    return {"count": len(results), "resolvedShipments": results}


def _build_return_address_from_db(db: Session) -> EkartAddress:
    try:
        from app.shipment.config_model import ShipmentConfigurations
        # FIX 2: stateId == "Active" is exact-match case-sensitive.
        # Fails when stateId is NULL, "active", or "ACTIVE".
        # Use case-insensitive OR NULL-safe filter.
        row = (
            db.query(ShipmentConfigurations)
            .filter(
                ShipmentConfigurations.deliveryAgent.ilike("EKart%"),
                ShipmentConfigurations.deletedInd == False,
                or_(
                    ShipmentConfigurations.stateId == None,  # noqa: E711
                    sa_func.lower(ShipmentConfigurations.stateId) == "active",
                ),
            )
            .first()
        )
        if row and row.returnAddressLine1:
            return EkartAddress(
                name=          row.returnName         or "Warehouse",
                phone=         row.returnPhone         or "",
                address_line1= row.returnAddressLine1  or "",
                address_line2= row.returnAddressLine2  or None,
                city=          row.returnCity          or "",
                state=         row.returnState         or "",
                pincode=       row.returnPinCode       or "",
                country=       row.returnCountry       or "India",
            )
    except Exception as e:
        print(f"[EkartRouter] DB return address load failed: {e}")

    return EkartAddress(
        name="Warehouse",
        phone="",
        address_line1="[FILL: Configure ReturnAddressLine1 in ShipmentConfigurations]",
        city="", state="", pincode="", country="India",
    )


def _build_shipment_request(order: dict, items: list) -> CreateShipmentRequest:
    req = CreateShipmentRequest(
        order_id=           order["order_id"],
        order_number=       order["order_number"],
        consignee_name=     order["name"],
        consignee_phone=    order["phone"] or "",
        consignee_address=EkartAddress(
            name=          order["name"] or "Customer",
            phone=         order["phone"] or "",
            address_line1= order["address_line"] or "",
            city=          order["city"] or "",
            state=         order["state_name"],
            pincode=       order["pincode"] or "",
            country=       order["country"],
        ),
        payment_mode=       "COD" if order["payment_mode"] == "COD" else "Prepaid",
        cod_amount=         order["total_amount"] if order["payment_mode"] == "COD" else None,
        total_amount=       order["total_amount"],
        items=              items,
        shipping_type_name= order.get("shipping_type_name", "Normal"),
    )
    print(
        f"[Ekart:_build_shipment_request] 📦 Request built for order {order['order_id']} | "
        f"payment_mode={req.payment_mode} | total=₹{req.total_amount} | "
        f"consignee={req.consignee_name!r} | phone={req.consignee_phone!r} | "
        f"city={req.consignee_address.city!r} | pin={req.consignee_address.pincode!r} | "
        f"state={req.consignee_address.state!r} | items={len(items)}"
    )
    return req


# ── Create Single Shipment ────────────────────────────────────────────────────

@router.post("/CreateShipment", response_model=ShipmentResponse)
async def create_shipment(
    inp: CreateShipmentInput,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_admin_user),
):
    print(f"\n{'='*60}")
    print(f"[Ekart:CreateShipment] ▶ Manual shipment request for order {inp.order_id}")
    print(f"[Ekart:CreateShipment]   manual_tracking_id={inp.manual_tracking_id!r}")

    order = _get_order_with_address(db, inp.order_id)
    if not order:
        print(f"[Ekart:CreateShipment] ❌ Order {inp.order_id} not found — aborting")
        raise HTTPException(404, "Order not found")

    print(f"[Ekart:CreateShipment] ✅ Order found: state={order['state']!r} tracking={order['tracking_id']!r}")

    if order["state"] in ("Shipped", "Delivered", "Cancelled"):
        print(f"[Ekart:CreateShipment] ⚠️  State is '{order['state']}' — cannot create shipment")
        raise HTTPException(400, f"Cannot create shipment for order in '{order['state']}' state")

    if order["tracking_id"]:
        print(f"[Ekart:CreateShipment] ℹ️  Already has tracking_id={order['tracking_id']!r} — skipping")
        return ShipmentResponse(
            success=True, order_id=order["order_id"],
            tracking_id=order["tracking_id"],
            message="Order already has a tracking ID",
        )

    if inp.manual_tracking_id:
        tracking_id  = inp.manual_tracking_id
        # Construct the full tracking URL the same way send_order_shipped_notification
        # does, so the admin UI can show/copy it immediately after manual entry.
        # Confirmed format: https://app.elite.ekartlogistics.in/track/{tracking_id}
        from app.shared.app_settings import get_setting as _get_setting
        _tracking_base = _get_setting("TRACKING_BASE_URL", "https://app.elite.ekartlogistics.in/track") or "https://app.elite.ekartlogistics.in/track"
        tracking_url = f"{_tracking_base.rstrip('/')}/{tracking_id}"
        print(f"[Ekart:CreateShipment] ℹ️  Using manual tracking_id={tracking_id!r} → {tracking_url}")
    else:
        items   = _get_order_items_for_shipment(db, inp.order_id)
        if not items:
            print(f"[Ekart:CreateShipment] ❌ No order items found for order {inp.order_id} — aborting")
            return ShipmentResponse(
                success=False, order_id=order["order_id"],
                error="No order items found. Cannot create Ekart shipment.",
            )

        request = _build_shipment_request(order, items)

        print(f"[Ekart:CreateShipment] 🔑 Loading Ekart config from DB...")
        client = get_ekart_client_for_db(db)
        print(f"[Ekart:CreateShipment] ℹ️  Client mode: {'MOCK ⚠️' if client.mock_mode else 'LIVE ✅'}")

        if client.mock_mode:
            print(
                f"[Ekart:CreateShipment] ❌ MOCK MODE — Ekart config is missing or incomplete.\n"
                f"   Check ShipmentConfigurations: needs clientId, userName, password,\n"
                f"   accessTokenURL, createShipmentURL, stateId=NULL or 'Active'.\n"
                f"   Run GET /api/Ekart/Readiness to see exactly what is missing."
            )
            return ShipmentResponse(
                success=False, order_id=order["order_id"],
                error="Ekart is in MOCK mode — config missing. Check /api/Ekart/Readiness.",
            )

        print(f"[Ekart:CreateShipment] 📡 Calling Ekart API (create_shipment)...")
        result = await client.create_shipment(request)

        if not result.success:
            print(
                f"[Ekart:CreateShipment] ❌ Ekart API FAILED for order {inp.order_id}: "
                f"{result.error_message}"
            )
            return ShipmentResponse(
                success=False, order_id=order["order_id"],
                error=result.error_message or "Failed to create shipment",
            )

        print(f"[Ekart:CreateShipment] ✅ Ekart API success: AWB={result.awb_number!r}")
        tracking_id  = result.awb_number
        tracking_url = result.tracking_url

    # ── Write tracking ID + state to DB ──────────────────────────────────────
    print(f"[Ekart:CreateShipment] 💾 Writing tracking_id={tracking_id!r} to DB for order {inp.order_id}...")
    try:
        db.execute(text("""
            UPDATE twam."Orders"
            SET "TrackingId" = :tid, "State" = 'Processing', "IsShipped" = true,
                "DeliveryAgent" = 'EKart', "ModifiedDate" = :now
            WHERE "OrderId" = :order_id AND "DeletedInd" = false
        """), {"tid": tracking_id, "order_id": inp.order_id, "now": datetime.now(timezone.utc)})

        db.add(OrderTrackingStatus(
            orderId=inp.order_id,
            status="Processing",
            createdDate=datetime.now(timezone.utc),
        ))
        db.commit()
        print(f"[Ekart:CreateShipment] ✅ DB updated — order {inp.order_id} → Processing, TrackingId={tracking_id!r}")

    except Exception as e:
        db.rollback()
        print(f"[Ekart:CreateShipment] ❌ DB commit FAILED for order {inp.order_id}: {e} — retrying once...")
        try:
            db.execute(text("""
                UPDATE twam."Orders"
                SET "TrackingId" = :tid, "State" = 'Processing',
                    "IsShipped" = true, "DeliveryAgent" = 'EKart',
                    "ModifiedDate" = :now
                WHERE "OrderId" = :order_id AND "DeletedInd" = false
            """), {"tid": tracking_id, "order_id": inp.order_id, "now": datetime.now(timezone.utc)})
            db.commit()
            print(f"[Ekart:CreateShipment] ✅ Retry commit succeeded for order {inp.order_id}")
        except Exception as e2:
            db.rollback()
            print(f"[Ekart:CreateShipment] ❌ Retry also FAILED: {e2}")
            raise HTTPException(500, f"Shipment created on Ekart (AWB: {tracking_id}) but DB update failed: {e2}")

    print(f"[Ekart:CreateShipment] 📣 Queuing shipped notification for order {inp.order_id}")
    background_tasks.add_task(send_order_shipped_notification, db, inp.order_id)

    # ── Deduct stock now that Ekart shipment is confirmed ─────────────────────
    print(f"[Ekart:CreateShipment] 📦 Deducting stock for order {inp.order_id}...")
    try:
        deduct_res = db.execute(text("""
            UPDATE twam."ProductVariantDetail" pvd
            SET "AvailableQuantity" = GREATEST(
                    0,
                    COALESCE(pvd."AvailableQuantity", 0) - oi.ordered_qty
                ),
                "ModifiedDate" = :now
            FROM (
                SELECT "ProductVariantDetailId",
                       SUM("Quantity") AS ordered_qty
                FROM   twam."OrderItems"
                WHERE  "OrderId"               = :oid
                  AND  "DeletedInd"             = false
                  AND  "ProductVariantDetailId" IS NOT NULL
                GROUP BY "ProductVariantDetailId"
            ) oi
            WHERE pvd."ProductVariantDetailId" = oi."ProductVariantDetailId"
        """), {"oid": inp.order_id, "now": datetime.now(timezone.utc)})
        db.commit()
        print(
            f"[Ekart:CreateShipment] ✅ Stock deducted for order {inp.order_id} — "
            f"{deduct_res.rowcount} variant(s) updated"
        )
    except Exception as _se:
        db.rollback()
        print(f"[Ekart:CreateShipment] ⚠️  Stock deduction FAILED (non-fatal) for order {inp.order_id}: {_se}")

    print(f"[Ekart:CreateShipment] ✅ DONE — order {inp.order_id} shipment created successfully\n{'='*60}\n")
    return ShipmentResponse(
        success=True, order_id=order["order_id"],
        tracking_id=tracking_id, tracking_url=tracking_url,
        message="Shipment created successfully",
    )


# ── Bulk Shipment Creation ────────────────────────────────────────────────────

@router.post("/BulkShipment")
async def create_bulk_shipments(
    inp: BulkShipmentInput,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_admin_user),
):
    """
    Create shipments for multiple orders in one call.
    Returns per-order success/failure results.
    """
    if not inp.order_ids:
        raise HTTPException(400, "No order IDs provided")

    if len(inp.order_ids) > 50:
        raise HTTPException(400, "Maximum 50 orders per bulk request")

    shipment_requests = []
    skipped = []

    for order_id in inp.order_ids:
        order = _get_order_with_address(db, order_id)
        if not order:
            skipped.append({"order_id": order_id, "reason": "Not found"})
            continue
        if order["state"] in ("Shipped", "Delivered", "Cancelled"):
            skipped.append({"order_id": order_id, "reason": f"State is '{order['state']}'"})
            continue
        if order["tracking_id"]:
            skipped.append({"order_id": order_id, "reason": "Already has tracking ID", "tracking_id": order["tracking_id"]})
            continue

        items = _get_order_items_for_shipment(db, order_id)
        if not items:
            skipped.append({"order_id": order_id, "reason": "No order items"})
            continue

        shipment_requests.append(_build_shipment_request(order, items))

    if not shipment_requests:
        return {"success": False, "processed": 0, "skipped": skipped, "results": []}

    client  = get_ekart_client_for_db(db)
    results = await client.create_bulk_shipments(shipment_requests)

    success_count = 0
    for r in results:
        if r["success"] and r["awb_number"]:
            db.execute(text("""
                UPDATE twam."Orders"
                SET "TrackingId" = :tid, "State" = 'Processing', "IsShipped" = true,
                    "DeliveryAgent" = 'EKart', "ModifiedDate" = :now
                WHERE "OrderId" = :order_id
            """), {
                "tid": r["awb_number"],
                "order_id": r["order_id"],
                "now": datetime.now(timezone.utc),
            })
            db.add(OrderTrackingStatus(
                orderId=r["order_id"], status="Processing",
                createdDate=datetime.now(timezone.utc),
            ))
            background_tasks.add_task(send_order_shipped_notification, db, r["order_id"])
            success_count += 1

    db.commit()

    return {
        "success":   success_count > 0,
        "processed": success_count,
        "failed":    len([r for r in results if not r["success"]]),
        "skipped":   skipped,
        "results":   results,
    }


# ── Create Return Shipment ────────────────────────────────────────────────────

@router.post("/CreateReturnShipment", response_model=ShipmentResponse)
async def create_return_shipment(
    inp: CreateReturnInput,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    order = _get_order_with_address(db, inp.order_id)
    if not order:
        raise HTTPException(404, "Order not found")

    if order["state"] != "Delivered":
        raise HTTPException(400, "Can only create return for delivered orders")

    items          = _get_order_items_for_shipment(db, inp.order_id)
    return_address = _build_return_address_from_db(db)

    request = CreateReturnShipmentRequest(
        order_id=       order["order_id"],
        original_awb=   order["tracking_id"] or "",
        return_reason=  inp.return_reason,
        pickup_name=    order["name"],
        pickup_phone=   order["phone"] or "",
        pickup_address= EkartAddress(
            name=          order["name"],
            phone=         order["phone"] or "",
            address_line1= order["address_line"] or "",
            city=          order["city"] or "",
            state=         order["state_name"],
            pincode=       order["pincode"] or "",
        ),
        return_address=return_address,
        items=items,
    )

    client = get_ekart_client_for_db(db)
    result = await client.create_return_shipment(request)

    if not result.success:
        return ShipmentResponse(
            success=False, order_id=order["order_id"],
            error=result.error_message or "Failed to create return shipment",
        )

    db.execute(text("""
        UPDATE twam."Orders"
        SET "State" = 'Return Initiated', "ModifiedDate" = :now
        WHERE "OrderId" = :order_id
    """), {"order_id": inp.order_id, "now": datetime.now(timezone.utc)})

    db.add(OrderTrackingStatus(
        orderId=inp.order_id, status="Return Initiated",
        createdDate=datetime.now(timezone.utc),
    ))
    db.commit()

    background_tasks.add_task(send_return_initiated_notification, db, inp.order_id, inp.return_reason)

    return ShipmentResponse(
        success=True, order_id=order["order_id"],
        tracking_id=result.awb_number, tracking_url=result.tracking_url,
        message="Return pickup scheduled",
    )


# ── Track Shipment ────────────────────────────────────────────────────────────

@router.get("/Track/{tracking_id}")
async def track_shipment(
    tracking_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    from app.shipment.repository import track_shipment_live
    return track_shipment_live(db, tracking_id)


# ── Cancel Shipment ───────────────────────────────────────────────────────────

@router.post("/CancelShipment", response_model=ShipmentResponse)
async def cancel_shipment(
    inp: CancelShipmentInput,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_admin_user),
):
    order = _get_order_with_address(db, inp.order_id)
    if not order:
        raise HTTPException(404, "Order not found")

    if order["state"] in ("Delivered", "Cancelled"):
        raise HTTPException(400, f"Cannot cancel order in '{order['state']}' state")

    if order["tracking_id"]:
        client = get_ekart_client_for_db(db)
        result = await client.cancel_shipment(CancelShipmentRequest(
            awb_number=order["tracking_id"],
            reason=inp.reason or "Customer requested cancellation",
        ))
        if not result.success:
            print(f"[Ekart] Cancel warning: {result.error_message}")

    db.execute(text("""
        UPDATE twam."Orders"
        SET "State" = 'Cancelled', "Reason" = :reason, "ModifiedDate" = :now
        WHERE "OrderId" = :order_id
    """), {"reason": inp.reason, "order_id": inp.order_id, "now": datetime.now(timezone.utc)})

    db.add(OrderTrackingStatus(
        orderId=inp.order_id, status="Cancelled",
        createdDate=datetime.now(timezone.utc),
    ))
    db.commit()

    background_tasks.add_task(send_order_cancelled_notification, db, inp.order_id, inp.reason)

    return ShipmentResponse(success=True, order_id=inp.order_id, message="Shipment cancelled")


# ── Update Order Status (Admin) ───────────────────────────────────────────────

@router.post("/UpdateStatus", response_model=ShipmentResponse)
async def update_order_status(
    inp: UpdateOrderStatusInput,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_admin_user),
):
    valid_statuses = [
        "Pending", "Confirmed", "Processing", "Shipped",
        "Out for Delivery", "Delivered", "Cancelled",
        "Return Initiated", "Returned", "Refunded"
    ]
    if inp.status not in valid_statuses:
        raise HTTPException(400, f"Invalid status. Must be one of: {', '.join(valid_statuses)}")

    order = db.query(Orders).filter(
        Orders.orderId == inp.order_id, Orders.deletedInd == False
    ).first()
    if not order:
        raise HTTPException(404, "Order not found")

    old_state   = order.state
    order.state = inp.status
    order.modifiedDate = datetime.now(timezone.utc)

    if inp.tracking_id:
        order.trackingId = inp.tracking_id
        order.isShipped  = True

    if inp.status == "Delivered" and not order.deliveredDate:
        order.deliveredDate = datetime.now(timezone.utc)

    db.add(OrderTrackingStatus(
        orderId=inp.order_id, status=inp.status,
        createdDate=datetime.now(timezone.utc),
    ))
    db.commit()

    if old_state != inp.status:
        background_tasks.add_task(send_order_status_notification, db, inp.order_id, inp.status)

    return ShipmentResponse(
        success=True, order_id=inp.order_id,
        tracking_id=order.trackingId,
        message=f"Order status updated to {inp.status}",
    )


# ── Download Label ────────────────────────────────────────────────────────────

@router.post("/DownloadLabel")
async def download_label(
    inp: LabelDownloadInput,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_admin_user),
):
    if not inp.awb_numbers:
        raise HTTPException(400, "No AWB numbers provided")
    if len(inp.awb_numbers) > 100:
        raise HTTPException(400, "Maximum 100 AWB numbers per label request")

    client = get_ekart_client_for_db(db)
    result = await client.download_label(inp.awb_numbers, json_only=inp.json_only)

    if isinstance(result, bytes):
        return Response(
            content=result,
            media_type="application/pdf",
            headers={"Content-Disposition": "attachment; filename=labels.pdf"},
        )
    else:
        return result


# ── Download Manifest ─────────────────────────────────────────────────────────

@router.post("/DownloadManifest")
async def download_manifest(
    inp: ManifestInput,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_admin_user),
):
    if not inp.awb_numbers:
        raise HTTPException(400, "No AWB numbers provided")
    if len(inp.awb_numbers) > 100:
        raise HTTPException(400, "Maximum 100 AWB numbers per manifest")

    client = get_ekart_client_for_db(db)
    result = await client.download_manifest(inp.awb_numbers)
    return result


# ── NDR Action ────────────────────────────────────────────────────────────────

@router.post("/NDRAction")
async def ndr_action(
    inp: NDRActionInput,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_admin_user),
):
    valid_actions = ["RE_ATTEMPT", "RTO", "CONFIRM_DELIVERY"]
    if inp.action not in valid_actions:
        raise HTTPException(400, f"Invalid NDR action. Must be one of: {', '.join(valid_actions)}")

    client = get_ekart_client_for_db(db)
    result = await client.action_ndr(
        tracking_id=inp.tracking_id,
        ndr_action=inp.action,
        remarks=inp.remarks or "",
        preferred_date=inp.preferred_date,
    )

    if inp.action == "RTO" and not result.get("error"):
        row = db.execute(
            text('SELECT "OrderId" FROM twam."Orders" WHERE "TrackingId" = :tid AND "DeletedInd" = false LIMIT 1'),
            {"tid": inp.tracking_id}
        ).fetchone()
        if row:
            db.execute(text("""
                UPDATE twam."Orders"
                SET "State" = 'Return Initiated', "ModifiedDate" = :now
                WHERE "OrderId" = :oid
            """), {"oid": row[0], "now": datetime.now(timezone.utc)})
            db.add(OrderTrackingStatus(
                orderId=row[0], status="Return Initiated",
                createdDate=datetime.now(timezone.utc),
            ))
            db.commit()

    return {"success": not result.get("error"), "tracking_id": inp.tracking_id, "result": result}


# ── Serviceability V2 ─────────────────────────────────────────────────────────

@router.get("/Serviceability/{pincode}")
async def check_serviceability(
    pincode: str,
    db: Session = Depends(get_db),
):
    if len(pincode) != 6 or not pincode.isdigit():
        return {"serviceable": False, "pincode": pincode, "message": "Invalid pincode (must be 6 digits)"}

    client = get_ekart_client_for_db(db)
    result = await client.check_serviceability_v2(pincode)
    return result


# ── Serviceability V3 ─────────────────────────────────────────────────────────

@router.post("/ServiceabilityV3")
async def check_serviceability_v3(
    inp: ServiceabilityV3Input,
    db: Session = Depends(get_db),
):
    client = get_ekart_client_for_db(db)
    result = await client.check_serviceability_v3(
        pickup_pincode=inp.pickup_pincode,
        drop_pincode=inp.drop_pincode,
        weight=inp.weight,
        length=inp.length,
        width=inp.width,
        height=inp.height,
    )
    return result


# ── Shipping Rate Calculator ──────────────────────────────────────────────────

@router.post("/ShippingEstimate")
async def get_shipping_estimate(
    inp: ShippingEstimateInput,
    db: Session = Depends(get_db),
):
    client = get_ekart_client_for_db(db)
    result = await client.get_shipping_estimate(
        pickup_pincode=inp.pickup_pincode,
        drop_pincode=inp.drop_pincode,
        weight=inp.weight,
        payment_mode=inp.payment_mode,
        cod_amount=inp.cod_amount,
    )
    return result


# ── Set Dispatch Date ─────────────────────────────────────────────────────────

@router.post("/SetDispatchDate")
async def set_dispatch_date(
    inp: DispatchDateInput,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_admin_user),
):
    if not inp.awb_numbers:
        raise HTTPException(400, "No AWB numbers provided")

    client = get_ekart_client_for_db(db)
    result = await client.set_dispatch_date(inp.awb_numbers, inp.dispatch_date)
    return result


# ── COD Collection Report ─────────────────────────────────────────────────────

@router.get("/CODReport")
def get_cod_report(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_admin_user),
):
    date_filter = ""
    params: dict = {}

    if start_date:
        date_filter += ' AND o."DeliveredDate" >= :start_date'
        params["start_date"] = start_date
    if end_date:
        date_filter += ' AND o."DeliveredDate" <= :end_date'
        params["end_date"] = end_date

    rows = db.execute(text(f"""
        SELECT
            o."OrderId", o."OrderNumber", o."TrackingId",
            o."TotalAmount", o."PaymentMethod", o."DeliveredDate",
            o."State", a."Name" AS customer_name,
            a."Phone" AS customer_phone, a."City"
        FROM twam."Orders" o
        LEFT JOIN twam."Address" a ON a."AddressId" = o."ShippingAddressId"
        WHERE o."PaymentMethod" = 'COD'
          AND o."State" = 'Delivered'
          AND o."DeletedInd" = false
          {date_filter}
        ORDER BY o."DeliveredDate" DESC
        LIMIT 500
    """), params).fetchall()

    items = [
        {
            "orderId":       r[0],
            "orderNumber":   r[1],
            "trackingId":    r[2],
            "codAmount":     float(r[3]) if r[3] else 0,
            "paymentMethod": r[4],
            "deliveredDate": r[5].isoformat() if r[5] else None,
            "state":         r[6],
            "customerName":  r[7],
            "customerPhone": r[8],
            "city":          r[9],
        }
        for r in rows
    ]

    return {
        "count":     len(items),
        "totalCOD":  sum(i["codAmount"] for i in items),
        "currency":  "INR",
        "startDate": start_date,
        "endDate":   end_date,
        "orders":    items,
    }


# ── Register Pickup Address with Ekart ───────────────────────────────────────

class RegisterAddressInput(BaseModel):
    alias: str                        # Short name to identify this warehouse e.g. "TWAM-HYD"
    phone: str
    address_line1: str
    pincode: str
    address_line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: str = "India"


@router.post("/RegisterAddress")
async def register_ekart_address(
    inp: RegisterAddressInput,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_admin_user),
):
    """
    Register your warehouse pickup/return address with Ekart (POST /api/v2/address).

    Call this ONCE after setting up ShipmentConfigurations. On success,
    Ekart registers the address under the given alias. The alias is then
    automatically saved to ShipmentConfigurations.PickupAlias so all future
    shipments send {"name": alias} instead of the full address object —
    resolving SWIFT_RESOURCE_NOT_FOUND_EXCEPTION.

    Use a meaningful alias like "TWAM-HYD-WH" (brand + city + warehouse).
    """
    client = get_ekart_client_for_db(db)
    result = await client.register_address(
        alias=inp.alias,
        phone=inp.phone,
        address_line1=inp.address_line1,
        pincode=inp.pincode,
        address_line2=inp.address_line2,
        city=inp.city,
        state=inp.state,
        country=inp.country,
    )

    if result.get("status"):
        # Save alias to ShipmentConfigurations so shipments use it automatically
        from app.shipment.config_model import ShipmentConfigurations
        from sqlalchemy import or_, func as sa_func
        row = db.query(ShipmentConfigurations).filter(
            ShipmentConfigurations.deliveryAgent.ilike("EKart%"),
            ShipmentConfigurations.deletedInd == False,
            or_(
                ShipmentConfigurations.stateId == None,  # noqa: E711
                sa_func.lower(ShipmentConfigurations.stateId) == "active",
            ),
        ).first()
        if row:
            row.pickupAlias = result.get("alias") or inp.alias
            row.modifiedDate = datetime.now(timezone.utc)
            db.commit()
            print(f"[Ekart] Pickup alias '{row.pickupAlias}' saved to ShipmentConfigurations")

    return {
        "success": result.get("status", False),
        "alias":   result.get("alias", inp.alias),
        "remark":  result.get("remark", ""),
        "alias_saved_to_config": result.get("status", False),
    }


@router.get("/RegisteredAddresses")
async def list_registered_addresses(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_admin_user),
):
    """
    List all pickup/return addresses registered with your Ekart account.
    Shows alias names you can use in shipment creation.
    Also shows the currently configured alias from ShipmentConfigurations.
    """
    client = get_ekart_client_for_db(db)
    addresses = await client.list_addresses()

    # Also show current alias from DB config
    from app.shipment.config_model import ShipmentConfigurations
    from sqlalchemy import or_, func as sa_func
    row = db.query(ShipmentConfigurations).filter(
        ShipmentConfigurations.deliveryAgent.ilike("EKart%"),
        ShipmentConfigurations.deletedInd == False,
        or_(
            ShipmentConfigurations.stateId == None,  # noqa: E711
            sa_func.lower(ShipmentConfigurations.stateId) == "active",
        ),
    ).first()

    return {
        "current_alias_in_config": row.pickupAlias if row else None,
        "alias_mode_active": bool(row and row.pickupAlias),
        "registered_addresses": addresses,
    }


# ── Admin: ShipmentConfigurations CRUD ───────────────────────────────────────

@router.get("/Config")
def get_shipment_config(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_admin_user),
):
    """View current shipping config from DB (password masked)."""
    from app.shipment.config_model import ShipmentConfigurations
    rows = db.query(ShipmentConfigurations).filter(
        ShipmentConfigurations.deletedInd == False
    ).all()

    return [
        {
            "id":              r.shipmentConfigurationId,
            "clientId":        r.clientId,
            "deliveryAgent":   r.deliveryAgent,
            "stateId":         r.stateId,
            "accessTokenURL":  r.accessTokenURL,
            "createShipmentURL": r.createShipmentURL,
            "cancelShipmentURL": r.cancelShipmentURL,
            "trackShipmentURL":  r.trackShipmentURL,
            "wayBillURL":      r.wayBillURL,
            "userName":        r.userName,
            "password":        "***" if r.password else None,
            "returnName":      r.returnName,
            "returnPhone":     r.returnPhone,
            "returnAddressLine1": r.returnAddressLine1,
            "returnAddressLine2": r.returnAddressLine2,
            "returnCity":      r.returnCity,
            "returnState":     r.returnState,
            "returnPinCode":   r.returnPinCode,
            "returnCountry":   r.returnCountry,
        }
        for r in rows
    ]


@router.put("/Config/{config_id}")
def update_shipment_config(
    config_id: int,
    payload: ShipmentConfigUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_admin_user),
):
    """Update Ekart credentials and warehouse return address in DB."""
    from app.shipment.config_model import ShipmentConfigurations
    row = db.query(ShipmentConfigurations).filter(
        ShipmentConfigurations.shipmentConfigurationId == config_id,
        ShipmentConfigurations.deletedInd == False,
    ).first()
    if not row:
        raise HTTPException(404, "Config not found")

    update_map = payload.dict(exclude_none=True)
    for field, value in update_map.items():
        attr = field[0].lower() + field[1:]
        if hasattr(row, attr):
            setattr(row, attr, value)

    row.modifiedDate = datetime.now(timezone.utc)
    db.commit()

    from app.ekart.client import _token_cache
    _token_cache.pop(row.clientId, None)

    return {"success": True, "message": "Shipment config updated"}


# ── Readiness Check ───────────────────────────────────────────────────────────

@router.get("/Readiness")
async def ekart_readiness(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_admin_user),
):
    """
    Check whether the Ekart integration is fully configured.
    Also checks SMS and SMTP configuration.
    """
    from app.shipment.config_model import ShipmentConfigurations

    # FIX 3: same stateId bug as _build_return_address_from_db — use NULL-safe filter.
    # Original stateId == "Active" caused Readiness to always report mock_mode=True
    # even when config was correctly set up with a different case or NULL StateId.
    row = db.query(ShipmentConfigurations).filter(
        ShipmentConfigurations.deliveryAgent.ilike("EKart%"),
        ShipmentConfigurations.deletedInd == False,
        or_(
            ShipmentConfigurations.stateId == None,  # noqa: E711
            sa_func.lower(ShipmentConfigurations.stateId) == "active",
        ),
    ).first()

    if not row:
        return {
            "ready_for_live": False,
            "mock_mode": True,
            "missing_required": ["No active EKart config in ShipmentConfigurations — run ekart_setup.sql"],
        }

    ekart_missing = []
    if not row.accessTokenURL:    ekart_missing.append("AccessTokenURL")
    if not row.createShipmentURL: ekart_missing.append("CreateShipmentURL")
    if not row.cancelShipmentURL: ekart_missing.append("CancelShipmentURL")
    if not row.trackShipmentURL:  ekart_missing.append("TrackShipmentURL")
    if not row.userName:           ekart_missing.append("UserName")
    if not row.password:           ekart_missing.append("Password")
    if not row.returnAddressLine1: ekart_missing.append("ReturnAddressLine1")
    if not row.returnCity:         ekart_missing.append("ReturnCity")
    if not row.returnPinCode:      ekart_missing.append("ReturnPinCode")
    if not row.returnPhone:        ekart_missing.append("ReturnPhone")

    sms_missing = []
    try:
        from app.sms.models import SmsCredential
        sms_cred = db.query(SmsCredential).filter(
            SmsCredential.isActive   == True,
            SmsCredential.deletedInd == False,
        ).first()
        if not sms_cred or not sms_cred.apiUrl:
            sms_missing.append("ApiUrl")
        if not sms_cred or not sms_cred.username:
            sms_missing.append("Username")
        if not sms_cred or not sms_cred.password:
            sms_missing.append("Password")
    except Exception:
        sms_missing.append("SmsCredential (table not found)")

    client = get_ekart_client_for_db(db)

    return {
        "ready_for_live":  len(ekart_missing) == 0 and not client.mock_mode,
        "mock_mode":       client.mock_mode,
        "ekart": {
            "missing": ekart_missing,
            "configured": {
                "auth":           bool(row.accessTokenURL),
                "create":         bool(row.createShipmentURL),
                "credentials":    bool(row.userName and row.password),
                "return_address": bool(row.returnAddressLine1 and row.returnPinCode),
            },
        },
        "sms": {
            "missing":    sms_missing,
            "configured": len(sms_missing) == 0,
        },
    }