import os
import random
import json
import re
from datetime import datetime, timezone
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import text, asc, desc
from app import orders
from app.shared.filters import apply_filters, apply_ordering, apply_pagination, build_paged_response

from .models import (
    Orders, OrderTrackingStatus,
    OrderReturnInfo, OrderRefund
)
from app.orderitems.models import OrderItems
from app.people.models import People
from app.auth.models import IdentityUser
from app.shared.app_settings import get_setting
from app.state.repository import resolve_state_id
from app.shared.url_helpers import resolve_url


def _get_payg_redirect_url(db: Session = None) -> str:
    """
    Return the PayG redirect (callback) URL from twam."AppSettings".
    Key: payg_redirect_url — set via the Admin → Payment Config panel
    or directly in the DB.

    If the in-process cache is empty (e.g. fresh container restart before
    the first admin save), falls back to querying the DB directly and
    refreshes the cache so subsequent calls are fast.

    Raises RuntimeError if the setting is still missing/unconfigured after
    the DB fallback, so misconfiguration fails loudly at order-creation time.
    """
    from app.shared.app_settings import reload_settings
    url = get_setting("payg_redirect_url", "")

    # Cache miss or stale default — try to reload from DB before giving up
    if (not url or "localhost" in url) and db is not None:
        try:
            reload_settings(db)
            url = get_setting("payg_redirect_url", "")
        except Exception as _reload_err:
            print(f"[PayG] Warning: cache reload failed: {_reload_err}")

    if not url or "localhost" in url:
        raise RuntimeError(
            "payg_redirect_url is not configured. "
            "Set it to your backend's public callback URL "
            "(e.g. https://your-backend.azurecontainerapps.io/api/Payment/Callback) "
            "via Admin → Payment Config or directly in twam.\"AppSettings\"."
        )
    return url

BASE_URL = os.getenv("BASE_URL", "")

# ── Helpers ───────────────────────────────────────────────────────────────────

def generate_order_number() -> str:
    return f"ORD{random.randint(1000, 9999)}"


def generate_order_item_number() -> str:
    return f"ORDI{random.randint(1000, 9999)}"


def _parse_min_order_value(order_value_range: Optional[str]) -> Optional[float]:
    if not order_value_range:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)", str(order_value_range).replace(",", ""))
    return float(match.group(1)) if match else None


# apply_filters, apply_ordering, apply_pagination imported from app.shared.filters


def _get_state_name(db: Session, state_id) -> Optional[str]:
    if not state_id:
        return None
    row = db.execute(
        text('SELECT "StateName" FROM mdm."State" WHERE "StateId" = :id LIMIT 1'),
        {"id": state_id},
    ).fetchone()
    return row[0] if row else None


def _get_address_state_name(db: Session, address_id: Optional[int]) -> Optional[str]:
    if not address_id:
        return None
    row = db.execute(
        text(
            """
            SELECT s."StateName"
            FROM twam."Address" a
            LEFT JOIN mdm."State" s ON s."StateId" = a."StateId"
            WHERE a."AddressId" = :id AND a."DeletedInd" = false
            LIMIT 1
            """
        ),
        {"id": address_id},
    ).fetchone()
    return row[0] if row else None


def _get_address(db: Session, address_id):
    if not address_id:
        return None
    result = db.execute(
        text('SELECT "AddressId","AddressLine","City","PinCode","Phone","Name" FROM twam."Address" WHERE "AddressId" = :id AND "DeletedInd" = false'),
        {"id": address_id}
    ).fetchone()
    if result:
        return {
            "addressId": result[0], "addressLine": result[1],
            "city": result[2], "pinCode": result[3],
            "phone": result[4], "name": result[5]
        }
    return None


def _get_owned_address(db: Session, address_id: Optional[int], user_profile_id: Optional[str] = None):
    if not address_id:
        return None

    sql = 'SELECT "AddressId","UserProfileId","AddressLine","City","PinCode","Phone","Name" FROM twam."Address" WHERE "AddressId" = :id AND "DeletedInd" = false'
    params = {"id": address_id}

    if user_profile_id:
        sql += ' AND "UserProfileId" = :user_profile_id'
        params["user_profile_id"] = user_profile_id

    result = db.execute(text(sql), params).fetchone()
    if result:
        return {
            "addressId": result[0],
            "userProfileId": result[1],
            "addressLine": result[2],
            "city": result[3],
            "pinCode": result[4],
            "phone": result[5],
            "name": result[6],
        }
    return None


def _build_address_response(addr: Optional[dict]) -> Optional[dict]:
    if not addr:
        return None
    return {
        "addressId": addr.get("addressId"),
        "addressLine": addr.get("addressLine"),
        "city": addr.get("city"),
        "pinCode": addr.get("pinCode"),
        "phone": addr.get("phone"),
        "name": addr.get("name"),
    }


def _get_order_items_with_details(db: Session, order_ids: List[int], include_reviews: bool = False):
    if not order_ids:
        return []
    ids_str = ",".join(str(i) for i in order_ids)
    print("DEBUG ids_str:", ids_str)
    sql = f"""
        SELECT
            oi."OrderItemId",
            oi."OrderId",
            oi."ProductVariantDetailId",
            oi."ProductId",
            oi."ProductVariantId",
            oi."Quantity",
            oi."Price",
            oi."OrderItemNumber",

            p."Name" AS product_name,
            b."brandName" AS brand_name,

            s."SizeLabel" AS size_label,
            cs."SizeLabel" AS cup_size_label,

            pv."Color" AS color,
            pv."IsReturnAvailable" AS is_return_available,

            p."ProductCode" AS sku,
                (
                    SELECT pi2."FilePath"
                    FROM twam."ProductImage" pi2
                    WHERE (
                        (
                            pi2."ProductVariantId" = pv."ProductVariantId"
                            AND pv."ProductVariantId" IS NOT NULL
                        )
                        OR
                        (
                            pi2."ProductId" = oi."ProductId"
                            AND pv."ProductVariantId" IS NULL
                        )
                    )
                    AND pi2."DeletedInd" = false
                    AND pi2."FilePath" IS NOT NULL
                    AND pi2."FilePath" != ''
                    LIMIT 1
                ) AS product_image

        FROM twam."OrderItems" oi

        LEFT JOIN twam."Products" p
            ON p."ProductId" = oi."ProductId"

        LEFT JOIN twam."ProductVariantDetail" pvd
            ON pvd."ProductVariantDetailId" = oi."ProductVariantDetailId"

        LEFT JOIN twam."ProductVariants" pv
            ON pv."ProductVariantId" = pvd."ProductVariantId"

        LEFT JOIN mdm."Size" s
            ON s."SizeId" = pvd."Size"

        LEFT JOIN mdm."CupSize" cs
            ON cs."CupSizeId" = pvd."CupSize"

        LEFT JOIN mdm."Brand" b
            ON b."brandId" = p."BrandId"

        WHERE
            oi."DeletedInd" = false
            AND (oi."IsReturn" IS NULL OR oi."IsReturn" = false)
            AND oi."OrderId" IN ({ids_str})
        """
    try:
        rows = db.execute(text(sql)).fetchall()
        print("DEBUG rows count:", len(rows))
    except Exception as e:
        print("DEBUG SQL ERROR:", e)  
        return []
    items = []
    for r in rows:
           items.append({
                    "orderItemId":            r[0],
                    "orderId":                r[1],
                    "productVariantDetailId": r[2],
                    "productId":              r[3],
                    "productVariantId":       r[4],
                    "quantity":               r[5],
                    "price":                  float(r[6]) if r[6] else None,
                    "orderItemNumber":        r[7],
                    "productName":            r[8],
                    "brandName":              r[9],   
                    "size":                   r[10],  
                    "cupSize":                r[11],  
                    "color":                  r[12],  
                    "isReturnAvailable":      r[13],  
                    "sku":                    r[14],  
                    "productImage":           resolve_url(r[15]) or "",
                    "rating": 0,
                    "comment": None
                })

    if include_reviews and items:
        item_ids = [i["orderItemId"] for i in items]
        ids_str2 = ",".join(str(i) for i in item_ids)
        reviews = {
            r[0]: {"rating": r[1], "comment": r[2]}
            for r in db.execute(text(
                f'SELECT "ItemId","Rating","Comment" FROM twam."ProductReview" WHERE "ItemId" IN ({ids_str2})'
            )).fetchall()
        }
        for item in items:
            rev = reviews.get(item["orderItemId"])
            if rev:
                item["rating"] = rev["rating"] or 0
                item["comment"] = rev["comment"]
    return items


def _get_delivery_info(db: Session, shipping_type_id, order_subtotal: Optional[float] = None):
    if not shipping_type_id:
        return None
    row = db.execute(
        text('SELECT "DeliveryChargeId","DeliveryCharges","Days","IsFree","Description","OrderValueRange" FROM twam."DeliveryCharge" WHERE "ShippingTypeId" = :id AND "DeletedInd" = false LIMIT 1'),
        {"id": shipping_type_id}
    ).fetchone()
    if row:
        min_order_value = _parse_min_order_value(row[5])
        subtotal = float(order_subtotal or 0)
        is_eligible = min_order_value is None or subtotal >= min_order_value
        unlock_amount = round(max((min_order_value or 0) - subtotal, 0), 2) if min_order_value is not None else 0.0
        return {
            "deliveryChargeId": row[0],
            "deliveryCharges": float(row[1]) if row[1] is not None else None,
            "days": row[2],
            "isFree": row[3],
            "description": row[4],
            "orderValueRange": row[5],
            "minOrderValue": min_order_value,
            "isEligible": is_eligible,
            "unlockAmount": unlock_amount,
        }
    return None


def _normalize_order_addresses_and_delivery(db: Session, data):
    user_profile_id = getattr(data, "userProfileId", None)
    shipping_address_id = getattr(data, "shippingAddressId", None)
    billing_address_id = getattr(data, "billingAddressId", None) or shipping_address_id

    if shipping_address_id:
        shipping_address = _get_owned_address(db, shipping_address_id, user_profile_id)
        if not shipping_address:
            raise ValueError("Shipping address not found for this user.")
    else:
        shipping_address = None

    if billing_address_id:
        billing_address = _get_owned_address(db, billing_address_id, user_profile_id)
        if not billing_address:
            raise ValueError("Billing address not found for this user.")
    else:
        billing_address = shipping_address

    if getattr(data, "shippingTypeId", None):
        delivery_info = _get_delivery_info(
            db,
            getattr(data, "shippingTypeId", None),
            getattr(data, "subTotal", None),
        )
        if delivery_info and delivery_info.get("isEligible") is False:
            min_value = delivery_info.get("minOrderValue")
            raise ValueError(f"This shipping option is available only for orders above {min_value:.0f}.")
        if getattr(data, "deliveryCharge", None) is None and delivery_info and delivery_info.get("deliveryCharges") is not None:
            data.deliveryCharge = delivery_info["deliveryCharges"]

    data.shippingAddressId = shipping_address["addressId"] if shipping_address else None
    data.billingAddressId = billing_address["addressId"] if billing_address else None

    return shipping_address, billing_address


def _get_cart_checkout_items(db: Session, cart_ids: List[int], user_profile_id: str) -> List[dict]:
    if not cart_ids:
        return []

    ids_str = ",".join(str(int(cart_id)) for cart_id in cart_ids)
    rows = db.execute(text(f"""
        SELECT
            c."CartId",
            c."ProductId",
            c."ProductVariantId",
            c."ProductVariantDetailId",
            c."Quantity",
            p."Name",
            pvd."FinalPrice",
            pvd."TaxAmount",
            pvd."CGST",
            pvd."SGST",
            (
                SELECT pi2."FilePath"
                FROM twam."ProductImage" pi2
                WHERE pi2."ProductVariantId" = c."ProductVariantId"
                  AND pi2."DeletedInd" = false
                  AND pi2."FilePath" IS NOT NULL
                  AND pi2."FilePath" != ''
                LIMIT 1
            ) AS image
        FROM twam."Cart" c
        LEFT JOIN twam."Products" p ON p."ProductId" = c."ProductId"
        LEFT JOIN twam."ProductVariantDetail" pvd ON pvd."ProductVariantDetailId" = c."ProductVariantDetailId"
        WHERE c."CartId" IN ({ids_str})
          AND c."UserProfileId" = :user_profile_id
          AND c."DeletedInd" = false
          AND (c."IsOrdered" IS NULL OR c."IsOrdered" = false)
    """), {"user_profile_id": user_profile_id}).fetchall()

    items = []
    for row in rows:
        quantity   = int(row[4] or 0)
        unit_price = float(row[6]) if row[6] is not None else 0.0
        unit_tax   = float(row[7]) if row[7] is not None else 0.0
        # FIX: Read stored CGST/SGST rates directly from ProductVariantDetail
        # instead of recomputing via TaxHSNCode. This ensures the cart checkout
        # summary always reflects the values that were stored at PVD creation time.
        stored_cgst_rate = float(row[8]) if row[8] is not None else 0.0
        stored_sgst_rate = float(row[9]) if row[9] is not None else 0.0
        product_id = row[1]

        # Compute per-unit tax amounts from stored rates
        cgst_unit = round(unit_price * stored_cgst_rate / 100, 2)
        sgst_unit = round(unit_price * stored_sgst_rate / 100, 2)
        # Fallback: if rates are 0 but taxAmount is stored, split 50/50
        if cgst_unit == 0 and sgst_unit == 0 and unit_tax > 0:
            cgst_unit = round(unit_tax / 2, 2)
            sgst_unit = round(unit_tax - cgst_unit, 2)

        items.append({
            "cartId":                 row[0],
            "productId":              product_id,
            "productVariantId":       row[2],
            "productVariantDetailId": row[3],
            "quantity":               quantity,
            "productName":            row[5],
            "unitPrice":              unit_price,
            "cgstRate":               stored_cgst_rate,
            "sgstRate":               stored_sgst_rate,
            "cgstAmount":             round(cgst_unit * quantity, 2),
            "sgstAmount":             round(sgst_unit * quantity, 2),
            "taxAmount":              round((cgst_unit + sgst_unit) * quantity, 2),
            "lineTotal":              round(unit_price * quantity, 2),
            "productImage":           resolve_url(row[10]) or "",
        })
    return items


def _get_buynow_checkout_items(db: Session, order_items: list) -> List[dict]:
    """
    Fetch live prices from DB for Buy Now inline items.
    This ensures prices shown in the checkout summary match the DB, not stale
    values that may have been passed from the client.
    Returns items in the same shape as _get_cart_checkout_items, but cartId=None.
    """
    if not order_items:
        return []

    pvd_ids = [i.productVariantDetailId for i in order_items if i.productVariantDetailId]
    if not pvd_ids:
        raise ValueError("Buy Now items must include productVariantDetailId.")

    ids_str = ",".join(str(i) for i in pvd_ids)
    rows = db.execute(text(f"""
        SELECT
            pvd."ProductVariantDetailId",
            pv."ProductId",
            pvd."ProductVariantId",
            p."Name",
            pvd."FinalPrice",
            pvd."TaxAmount",
            (
                SELECT pi2."FilePath"
                FROM twam."ProductImage" pi2
                WHERE pi2."ProductVariantId" = pvd."ProductVariantId"
                  AND pi2."DeletedInd" = false
                  AND pi2."FilePath" IS NOT NULL
                  AND pi2."FilePath" != ''
                LIMIT 1
            ) AS image
        FROM twam."ProductVariantDetail" pvd
        LEFT JOIN twam."ProductVariants" pv ON pv."ProductVariantId" = pvd."ProductVariantId"
        LEFT JOIN twam."Products" p ON p."ProductId" = pv."ProductId"
        WHERE pvd."ProductVariantDetailId" IN ({ids_str})
          AND pvd."DeletedInd" = false
    """)).fetchall()

    # Build a lookup keyed by PVD ID
    db_map = {row[0]: row for row in rows}

    items = []
    for item in order_items:
        pvd_id = item.productVariantDetailId
        if pvd_id not in db_map:
            raise ValueError(f"Product variant detail {pvd_id} not found or unavailable.")
        row        = db_map[pvd_id]
        quantity   = int(item.quantity or 1)
        unit_price = float(row[4]) if row[4] is not None else 0.0
        product_id = row[1]

        try:
            from app.taxhsncode.repository import compute_tax_for_product
            tax = compute_tax_for_product(db, product_id, unit_price)
        except Exception:
            tax = {"cgstRate": 0.0, "sgstRate": 0.0, "cgstAmount": 0.0, "sgstAmount": 0.0}

        items.append({
            "cartId":                 None,
            "productId":              product_id,
            "productVariantId":       row[2],
            "productVariantDetailId": pvd_id,
            "quantity":               quantity,
            "productName":            row[3],
            "unitPrice":              unit_price,
            "cgstRate":               tax["cgstRate"],
            "sgstRate":               tax["sgstRate"],
            "cgstAmount":             round(tax["cgstAmount"] * quantity, 2),
            "sgstAmount":             round(tax["sgstAmount"] * quantity, 2),
            "taxAmount":              round((tax["cgstAmount"] + tax["sgstAmount"]) * quantity, 2),
            "lineTotal":              round(unit_price * quantity, 2),
            "productImage":           resolve_url(row[6]) or "",
        })
    return items


def get_checkout_summary(db: Session, user_profile_id: str, data) -> dict:
    if not user_profile_id:
        raise ValueError("User profile is required.")

    # ── Determine mode: Buy Now (inline items) vs regular cart ──────────────
    inline_items = getattr(data, "orderItems", None) or []
    cart_ids = list(dict.fromkeys(getattr(data, "cartIds", None) or []))
    is_buy_now = bool(inline_items)

    if not is_buy_now and not cart_ids:
        raise ValueError("At least one cart item is required.")

    shipping_address = _get_owned_address(db, data.shippingAddressId, user_profile_id) if data.shippingAddressId else None
    if data.shippingAddressId and not shipping_address:
        raise ValueError("Shipping address not found for this user.")

    billing_source_id = data.billingAddressId or data.shippingAddressId
    billing_address = _get_owned_address(db, billing_source_id, user_profile_id) if billing_source_id else shipping_address
    if billing_source_id and not billing_address:
        raise ValueError("Billing address not found for this user.")

    if is_buy_now:
        # Buy Now path: fetch live prices from DB, no cart lookup
        cart_items = _get_buynow_checkout_items(db, inline_items)
    else:
        # Regular cart path
        cart_items = _get_cart_checkout_items(db, cart_ids, user_profile_id)
        if len(cart_items) != len(cart_ids):
            raise ValueError("Some cart items were not found for this user.")

    sub_total   = round(sum(item["lineTotal"]   for item in cart_items), 2)
    tax_amount  = round(sum(item["taxAmount"]   for item in cart_items), 2)
    cgst_total  = round(sum(item.get("cgstAmount", 0) for item in cart_items), 2)
    sgst_total  = round(sum(item.get("sgstAmount", 0) for item in cart_items), 2)
    delivery_info = _get_delivery_info(db, data.shippingTypeId, sub_total)
    if delivery_info and delivery_info.get("isEligible") is False:
        min_value = delivery_info.get("minOrderValue")
        raise ValueError(f"This shipping option is available only for orders above {min_value:.0f}.")
    delivery_charge = round(float(delivery_info["deliveryCharges"]) if delivery_info and delivery_info.get("deliveryCharges") is not None else 0.0, 2)
    coupon_amount = round(float(data.couponAmount or 0), 2)
    total_amount = round(sub_total + delivery_charge - coupon_amount, 2)

    return {
        "shippingAddress": _build_address_response(shipping_address),
        "billingAddress":  _build_address_response(billing_address),
        "deliveryInfo":    delivery_info,
        "shippingTypeId":  data.shippingTypeId,
        "cartItems":       cart_items,
        "itemCount":       len(cart_items),
        "totalQuantity":   sum(item["quantity"] for item in cart_items),
        "subTotal":        sub_total,
        "taxAmount":       tax_amount,
        "cgstTotal":       cgst_total,
        "sgstTotal":       sgst_total,
        "deliveryCharge":  delivery_charge,
        "couponAmount":    coupon_amount,
        "totalAmount":     total_amount,
        "isBuyNow":        is_buy_now,
    }


# ── Check Out of Stock ────────────────────────────────────────────────────────

def check_out_of_stock(db: Session, product_variant_detail_ids: List[int]) -> List[dict]:
    """
    Check which product variant details are out of stock.
    Uses direct SQL against ProductVariantDetail.IsOutOfStock instead of the
    missing stored procedure twam.SP_CheckProductStock.
    IMPORTANT: rolls back on any DB error so the transaction stays usable.
    """
    if not product_variant_detail_ids:
        return []
    ids_str = ",".join(str(i) for i in product_variant_detail_ids)
    try:
        rows = db.execute(
            text(f"""
                SELECT pvd."ProductVariantDetailId", p."Name"
                FROM twam."ProductVariantDetail" pvd
                LEFT JOIN twam."ProductVariants" pv  ON pv."ProductVariantId"  = pvd."ProductVariantId"
                LEFT JOIN twam."Products"         p   ON p."ProductId"          = pv."ProductId"
                WHERE pvd."ProductVariantDetailId" IN ({ids_str})
                  AND pvd."DeletedInd" = false
                  AND (pvd."IsOutOfStock" = true OR COALESCE(pvd."AvailableQuantity", 1) <= 0)
            """)
        ).fetchall()
        return [{"productVariantDetailId": r[0], "productName": r[1]} for r in rows]
    except Exception as e:
        print(f"[Order] check_out_of_stock query failed: {e}")
        # Roll back so the transaction is usable for subsequent queries
        try:
            db.rollback()
        except Exception:
            pass
        return []  # Treat as in-stock and let order proceed


# ── Create Order ──────────────────────────────────────────────────────────────

def create_order(db: Session, data) -> dict:
    # Accept both Pydantic model and dict
    if isinstance(data, dict):
        from types import SimpleNamespace
        data = SimpleNamespace(**data)
        data.orderItems = [
            SimpleNamespace(**i) if isinstance(i, dict) else i
            for i in (data.orderItems or [])
        ]

    order_items_input = data.orderItems or []

    # REMOVED FALLBACK: The old code had a dangerous fallback here that, when
    # orderItems was empty, fetched ALL unordered cart items for the submitted
    # cartIds via get_checkout_summary().  This caused phantom products to
    # appear in the order when the frontend accidentally sent an empty
    # orderItems list but a full cartId list (e.g. after a session reset).
    #
    # The frontend (Payment.tsx) always sends an explicit orderItems list built
    # from the checkout session, so the fallback is never needed.  Removing it
    # means an empty orderItems list is now a hard error, which surfaces the
    # real problem instead of silently shipping the wrong products.
    if not order_items_input:
        raise ValueError(
            "orderItems must not be empty. "
            "The checkout session may have been lost — please return to cart and try again."
        )

    # ── FIX: Cross-validate orderItems against the submitted cartIds ────────
    # This is the definitive server-side guard against phantom products.
    #
    # The frontend sends BOTH:
    #   - cartId: list of CartIds the user selected in the cart UI
    #   - orderItems: list of products (with productVariantDetailId) to order
    #
    # Before this fix, create_order trusted orderItems blindly.  If a stale
    # checkout session or a UI race condition included an unselected product in
    # orderItems, it would silently appear in the order.
    #
    # Now we fetch the ProductVariantDetailIds that are authorised for this
    # checkout (i.e., belong to the submitted cartIds and belong to this user)
    # and reject any orderItems entry whose PVDID is not in that authorised set.
    #
    # If no cartIds were submitted (Buy Now flow), skip this check — Buy Now
    # items have no Cart rows.
    submitted_cart_ids = list({int(c) for c in (getattr(data, "cartId", None) or []) if c})
    if submitted_cart_ids:
        user_profile_id_str = getattr(data, "userProfileId", None) or ""
        ids_str_cids = ",".join(str(c) for c in submitted_cart_ids)
        authorised_rows = db.execute(text(f"""
            SELECT "CartId", "ProductVariantDetailId"
            FROM twam."Cart"
            WHERE "CartId" IN ({ids_str_cids})
              AND "UserProfileId" = :uid
              AND "DeletedInd" = false
              AND ("IsOrdered" IS NULL OR "IsOrdered" = false)
        """), {"uid": user_profile_id_str}).fetchall()

        # Map: PVDID → CartId (only authorised PVDIDs)
        authorised_pvdids = {row[1] for row in authorised_rows if row[1] is not None}

        phantom = [
            i.productVariantDetailId
            for i in order_items_input
            if i.productVariantDetailId is not None
            and i.productVariantDetailId not in authorised_pvdids
        ]
        if phantom:
            print(
                f"[Order] ❌ Phantom product guard triggered — "
                f"orderItems contains PVDIDs not in submitted cartIds: {phantom}. "
                f"Submitted cartIds={submitted_cart_ids}. "
                f"Authorised PVDIDs={authorised_pvdids}. "
                f"Rejecting order to prevent incorrect products being charged."
            )
            raise ValueError(
                "One or more items in your order could not be verified. "
                "Please return to cart, review your selection, and try again."
            )

    variant_ids = list({i.productVariantDetailId for i in order_items_input if i.productVariantDetailId})

    out_of_stock = check_out_of_stock(db, variant_ids)
    if out_of_stock:
        return {"outOfStockProducts": out_of_stock}

    shipping_address, billing_address = _normalize_order_addresses_and_delivery(db, data)

    # ── FIX: Fetch live prices from DB BEFORE creating the order so that
    # subTotal / totalAmount are always authoritative server-side values.
    # This prevents a mismatch where the client submits wrong amounts (e.g.
    # sending a lower total while including extra items in the payload).
    pvd_map = {}
    if order_items_input:
        pvd_ids = [i.productVariantDetailId for i in order_items_input if i.productVariantDetailId]
        if pvd_ids:
            ids_str = ",".join(str(i) for i in pvd_ids)
            rows = db.execute(text(
                f'SELECT "ProductVariantDetailId","CGST","SGST","TaxAmount","FinalPrice" '
                f'FROM twam."ProductVariantDetail" '
                f'WHERE "ProductVariantDetailId" IN ({ids_str})'
            )).fetchall()
            pvd_map = {r[0]: {"cgst": r[1], "sgst": r[2], "taxAmount": r[3], "finalPrice": r[4]} for r in rows}

    # ── Server-side total recalculation ────────────────────────────────────
    # Always derive from live DB prices, never from client-submitted values.
    computed_subtotal = round(sum(
        float(pvd_map.get(x.productVariantDetailId, {}).get("finalPrice") or 0) * int(x.quantity or 1)
        for x in order_items_input
    ), 2)
    computed_tax = round(sum(
        float(pvd_map.get(x.productVariantDetailId, {}).get("taxAmount") or 0) * int(x.quantity or 1)
        for x in order_items_input
    ), 2)
    computed_delivery = round(float(getattr(data, "deliveryCharge", 0) or 0), 2)
    computed_coupon   = round(float(getattr(data, "couponAmount", 0) or 0), 2)
    computed_total    = round(computed_subtotal + computed_delivery - computed_coupon, 2)

    print(
        f"[Order] Server-side totals — subtotal=₹{computed_subtotal} "
        f"tax=₹{computed_tax} delivery=₹{computed_delivery} "
        f"coupon=₹{computed_coupon} total=₹{computed_total} "
        f"(client sent subTotal={getattr(data, 'subTotal', '?')} "
        f"totalAmount={getattr(data, 'totalAmount', '?')})"
    )

    order = Orders(
        orderNumber=generate_order_number(),
        totalAmount=computed_total,
        orderDate=getattr(data, "orderDate", None) or datetime.now(timezone.utc),
        userProfileId=getattr(data, "userProfileId", None),
        state="AwaitingPayment",
        createdDate=datetime.now(timezone.utc),
        deletedInd=False,
        shippingAddressId=getattr(data, "shippingAddressId", None),
        billingAddressId=getattr(data, "billingAddressId", None),
        personalId=0,
        couponId=getattr(data, "couponId", None),
        shippingTypeId=getattr(data, "shippingTypeId", None),
        couponAmount=computed_coupon,
        deliveryCharge=computed_delivery,
        taxAmount=computed_tax,
        subTotal=computed_subtotal,
        isWhatsappNotification=getattr(data, "isWhatsappNotification", None),
        paymentMethod=getattr(data, "paymentMethod", None),
        paymentAccount=getattr(data, "paymentAccount", None),
        platform=getattr(data, "platform", None),
    )
    db.add(order)
    db.flush()  # get auto-generated orderId
    order_id = order.orderId

    # Save order items (pvd_map already built above — no second DB round-trip)
    for x in order_items_input:
        variant     = pvd_map.get(x.productVariantDetailId, {})
        tax         = float(variant.get("taxAmount") or 0)
        final_price = float(variant.get("finalPrice") or 0)

        item = OrderItems(
            OrderId                = order_id,
            OrderItemNumber        = generate_order_item_number(),
            ProductVariantDetailId = x.productVariantDetailId,
            ProductVariantId       = x.productVariantId,
            ProductId              = x.productId,
            Quantity               = x.quantity,
            Price                  = final_price,        # ← use live DB price, not client value
            CreatedDate            = datetime.now(timezone.utc),
            DeletedInd             = False,
            TaxAmount              = tax,
            CGST                   = variant.get("cgst"),
            SGST                   = variant.get("sgst"),
            UnitPrice              = round(final_price - tax, 2) if final_price else None,
        )
        db.add(item)

    # Add initial tracking status
    db.add(OrderTrackingStatus(
        orderId=order_id,
        status="AwaitingPayment",
        createdDate=datetime.now(timezone.utc)
    ))

    # ── Surprise gift: link applicable rule to this order ────────────────────
    surprise_gift_rule_id = getattr(data, "surpriseGiftRuleId", None)
    if surprise_gift_rule_id:
        try:
            from app.surprisegift.models import SurpriseGiftRule
            from app.surprisegift.repository import attach_gift_to_order
            rule = (
                db.query(SurpriseGiftRule)
                .filter(
                    SurpriseGiftRule.giftRuleId == surprise_gift_rule_id,
                    SurpriseGiftRule.deletedInd == False,
                    SurpriseGiftRule.isActive == True,
                )
                .first()
            )
            if rule:
                attach_gift_to_order(db, order_id, rule)
                print(f"[Order] 🎁 Surprise gift rule #{rule.giftRuleId} ({rule.giftType}) linked to order {order_id}")
            else:
                print(f"[Order] ⚠️  surpriseGiftRuleId={surprise_gift_rule_id} not found or inactive — skipping gift")
        except Exception as _gift_exc:
            # Non-fatal — don't block the order if gift lookup fails
            print(f"[Order] ⚠️  Failed to attach surprise gift to order {order_id}: {_gift_exc}")

    # ── FIX: do NOT commit yet — PayG must succeed before the order is saved.
    # We only flush here to get orderId from the DB sequence; the full commit
    # happens inside the PayG success path below.  If PayG fails we rollback
    # so the order never appears in the database and the user gets a real error.
    db.flush()

    # ── PayG payment initiation ──────────────────────────────────────────────
    # NOTE: Order placed notification fires ONLY after payment confirmation
    # in app/payment/router.py -> _send_payment_notification(), not here.
    # Firing here would send confirmation BEFORE payment is approved.
    from app.payment.payg_client import create_payg_order

    # Fetch customer info while still inside the open transaction
    _user = (
        db.query(
            IdentityUser.Email,
            People.firstName,
            People.lastName,
            IdentityUser.PhoneNumber,
        )
        .join(People, IdentityUser.Id == People.userProfileId)
        .filter(
            People.userProfileId == getattr(data, "userProfileId", None),
            People.deletedInd == False,
        )
        .first()
    )

    payg_address = billing_address or shipping_address or _get_address(db, getattr(data, "shippingAddressId", None))
    payg_billing_state = str(getattr(data, "billingState", "") or "").strip()
    if not payg_billing_state:
        payg_billing_state = str(
            _get_address_state_name(
                db,
                getattr(data, "billingAddressId", None) or getattr(data, "shippingAddressId", None),
            )
            or ""
        ).strip()

    payg_shipping_state = str(getattr(data, "shippingState", "") or "").strip()
    if not payg_shipping_state:
        payg_shipping_state = str(
            _get_address_state_name(db, getattr(data, "shippingAddressId", None))
            or ""
        ).strip()
    if not payg_shipping_state:
        payg_shipping_state = payg_billing_state

    _payment_mode = str(getattr(data, "paymentMode", "") or "").upper()

    try:
        if _payment_mode == "UPI":
            from app.payment.payg_client import create_payg_upi_intent_order
            _payg = create_payg_upi_intent_order(
                order_id       = order_id,
                order_number   = order.orderNumber,
                amount         = float(getattr(data, "totalAmount", 0) or 0),
                customer_name  = (
                    ((_user[1] or "") + " " + (_user[2] or "")).strip()
                    if _user else "Customer"
                ),
                customer_email = _user[0] if _user else "",
                customer_phone = (
                    (payg_address or {}).get("phone") or (_user[3] if _user else "")
                ),
                billing_address = (payg_address or {}).get("addressLine", ""),
                billing_city    = (payg_address or {}).get("city", ""),
                billing_state   = payg_billing_state or "",
                shipping_state  = payg_shipping_state or payg_billing_state or "",
                billing_zip     = (payg_address or {}).get("pinCode", ""),
                redirect_url    = _get_payg_redirect_url(db),
            )
        else:
            _payg = create_payg_order(
                order_id       = order_id,
                order_number   = order.orderNumber,
                amount         = float(getattr(data, "totalAmount", 0) or 0),
                customer_name  = (
                    ((_user[1] or "") + " " + (_user[2] or "")).strip()
                    if _user else "Customer"
                ),
                customer_email = _user[0] if _user else "",
                customer_phone = (
                    (payg_address or {}).get("phone") or (_user[3] if _user else "")
                ),
                billing_address = (payg_address or {}).get("addressLine", ""),
                billing_city    = (payg_address or {}).get("city", ""),
                billing_state   = payg_billing_state or "",
                shipping_state  = payg_shipping_state or payg_billing_state or "",
                billing_zip     = (payg_address or {}).get("pinCode", ""),
                redirect_url    = _get_payg_redirect_url(db),
                # "CARD" (mobile card button) → DB|CC only, hides QR/UPI.
                # empty/None (website, unchanged) → "" → combined screen.
                accepted_payment_types = "DB|CC" if _payment_mode == "CARD" else "",
            )
    except Exception as _exc:
        # ── PayG failed: roll back everything so no orphan order is saved ──
        import traceback
        print(f"[Payment] ❌ PayG initiation FAILED — rolling back order {order_id}:")
        print(traceback.format_exc())
        db.rollback()
        raise RuntimeError(str(_exc)) from _exc

    # ── PayG succeeded: commit order + set payment ref ───────────────────────
    payment_url = _payg.get("ProcessingUrl") or _payg.get("PaymentProcessUrl")
    order.paymentTransactionRefNo = _payg.get("_unique_request_id", "")
    order.orderKeyId              = _payg.get("OrderKeyId", "")   # FIX: store for Detail API lookup
    db.add(order)
    db.commit()

    # NOTE: Cart items are marked IsOrdered=true ONLY after payment is confirmed
    # successful in app/payment/router.py (payment_callback success path).
    # Marking them here would hide items from the cart even if payment fails,
    # preventing the user from retrying with those same products.

    return {"orderId": order_id, "paymentProcessUrl": payment_url, "upiLink": _payg.get("UpiLink")}


# ── ReOrder (Cart) ────────────────────────────────────────────────────────────

def reorder_from_cart(db: Session, order_id: int, user_profile_id: str) -> int:
    payload = json.dumps({"OrderId": str(order_id), "UserProfileId": user_profile_id})
    try:
        db.execute(text("CALL twam.SP_AutoCreateCart(:cart_search)"), {"cart_search": payload})
        db.commit()
    except Exception:
        db.rollback()
    return order_id


# ── Guest Order ───────────────────────────────────────────────────────────────

def create_guest_order(db: Session, data) -> dict:
    # TODO: Replace stub with IIdentityUserService.CreateIdentityUserAsync
    user_profile_id = getattr(data, "userProfileId", None) or f"guest_{random.randint(10000, 99999)}"

    order_items_input = data.orderItems or []
    variant_ids = list({i.productVariantDetailId for i in order_items_input if i.productVariantDetailId})
    out_of_stock = check_out_of_stock(db, variant_ids)
    if out_of_stock:
        return {"outOfStockProducts": out_of_stock}

    # Create address
    address = {
        "name": f"{data.firstName or ''} {data.lastName or ''}".strip(),
        "addressLine": data.addressLine, "locality": data.locality, "city": data.city,
        "stateId": _state_id, "countryId": data.countryId, "pinCode": data.pinCode,
        "phone": data.phoneNumber, "isDefault": data.isDefault or False,
        "userProfileId": user_profile_id, "typeId": data.typeId,
        "createdDate": datetime.now(timezone.utc), "deletedInd": False,
        "isBillingAddress": data.isBillingAddress
    }
    result = db.execute(text("""
        INSERT INTO twam."Address"
        ("Name","AddressLine","Locality","City","StateId","CountryId","PinCode","Phone",
         "IsDefault","UserProfileId","TypeId","CreatedDate","DeletedInd","IsBillingAddress")
        VALUES (:name,:addressLine,:locality,:city,:stateId,:countryId,:pinCode,:phone,
                :isDefault,:userProfileId,:typeId,:createdDate,:deletedInd,:isBillingAddress)
        RETURNING "AddressId"
    """), address)
    address_id = result.fetchone()[0]
    db.commit()

    if data.shippingTypeId and data.deliveryCharge is None:
        delivery_info = _get_delivery_info(db, data.shippingTypeId)
        if delivery_info and delivery_info.get("deliveryCharges") is not None:
            data.deliveryCharge = delivery_info["deliveryCharges"]

    order = Orders(
        orderNumber=generate_order_number(),
        totalAmount=data.totalAmount,
        orderDate=getattr(data, "orderDate", None) or datetime.now(timezone.utc),
        userProfileId=user_profile_id,
        state="Pending",
        createdDate=datetime.now(timezone.utc),
        deletedInd=False,
        shippingAddressId=address_id,
        billingAddressId=address_id,
        personalId=0,
        couponId=data.couponId,
        shippingTypeId=data.shippingTypeId,
        couponAmount=data.couponAmount,
        deliveryCharge=data.deliveryCharge,
        taxAmount=data.taxAmount,
        subTotal=data.subTotal,
        isWhatsappNotification=data.isWhatsappNotification,
    )
    db.add(order)
    db.flush()
    order_id = order.orderId

    pvd_map = {}
    if order_items_input:
        pvd_ids = [i.productVariantDetailId for i in order_items_input if i.productVariantDetailId]
        if pvd_ids:
            ids_str = ",".join(str(i) for i in pvd_ids)
            rows = db.execute(text(
                f'SELECT "ProductVariantDetailId","CGST","SGST","TaxAmount","FinalPrice" FROM twam."ProductVariantDetail" WHERE "ProductVariantDetailId" IN ({ids_str})'
            )).fetchall()
            pvd_map = {r[0]: {"cgst": r[1], "sgst": r[2], "taxAmount": r[3], "finalPrice": r[4]} for r in rows}

        total_tax = 0
        for x in order_items_input:
            variant     = pvd_map.get(x.productVariantDetailId, {})
            tax         = float(variant.get("taxAmount") or 0)
            final_price = float(variant.get("finalPrice") or 0)
            total_tax  += tax

            # ── BUG FIX: use PascalCase attribute names ──────────────────────
            db.add(OrderItems(
                OrderId                = order_id,
                OrderItemNumber        = generate_order_item_number(),
                ProductVariantDetailId = x.productVariantDetailId,
                ProductVariantId       = x.productVariantId,
                ProductId              = x.productId,
                Quantity               = x.quantity,
                Price                  = x.price,
                CreatedDate            = datetime.now(timezone.utc),
                DeletedInd             = False,
                TaxAmount              = tax,
                CGST                   = variant.get("cgst"),
                SGST                   = variant.get("sgst"),
                UnitPrice              = final_price - tax if final_price else None,
            ))
        order.taxAmount = total_tax

    # ── FIX: flush only — do NOT commit until PayG confirms ─────────────────
    db.flush()

    # ── PayG payment initiation (guest order) ────────────────────────────────
    from app.payment.payg_client import create_payg_order
    from app.shared.app_settings import get_setting

    _addr_line = getattr(data, "addressLine", "") or ""
    _city      = getattr(data, "city", "") or ""
    _zip       = getattr(data, "pinCode", "") or ""
    _phone     = getattr(data, "phone", "") or ""
    _email     = getattr(data, "email", "") or ""
    _name      = f"{getattr(data, 'firstName', '') or ''} {getattr(data, 'lastName', '') or ''}".strip()
    _state_id = resolve_state_id(db, getattr(data, "stateId", None), getattr(data, "state", None))
    if _state_id is None and getattr(data, "stateId", None) is not None:
        _state_id = getattr(data, "stateId", None)
    _state_name = _get_state_name(db, _state_id) or getattr(data, "state", "") or ""

    _payment_mode = str(getattr(data, "paymentMode", "") or "").upper()

    try:
        if _payment_mode == "UPI":
            from app.payment.payg_client import create_payg_upi_intent_order
            _payg = create_payg_upi_intent_order(
                order_id        = order_id,
                order_number    = order.orderNumber,
                amount          = float(getattr(data, "totalAmount", 0) or 0),
                customer_name   = _name or "Guest",
                customer_email  = _email,
                customer_phone  = _phone,
                billing_address = _addr_line,
                billing_city    = _city,
                billing_state   = _state_name,
                shipping_state  = _state_name,
                billing_zip     = _zip,
                redirect_url    = _get_payg_redirect_url(db),
            )
        else:
            _payg = create_payg_order(
                order_id        = order_id,
                order_number    = order.orderNumber,
                amount          = float(getattr(data, "totalAmount", 0) or 0),
                customer_name   = _name or "Guest",
                customer_email  = _email,
                customer_phone  = _phone,
                billing_address = _addr_line,
                billing_city    = _city,
                billing_state   = _state_name,
                shipping_state  = _state_name,
                billing_zip     = _zip,
                redirect_url    = _get_payg_redirect_url(db),
                accepted_payment_types = "DB|CC" if _payment_mode == "CARD" else "",
            )
    except Exception as _exc:
        import traceback
        print(f"[Payment] ❌ PayG initiation FAILED — rolling back guest order {order_id}:")
        print(traceback.format_exc())
        db.rollback()
        raise RuntimeError(str(_exc)) from _exc

    # ── PayG succeeded: commit ───────────────────────────────────────────────
    payment_url = _payg.get("ProcessingUrl") or _payg.get("PaymentProcessUrl")
    order.paymentTransactionRefNo = _payg.get("_unique_request_id", "")
    order.orderKeyId              = _payg.get("OrderKeyId", "")   # FIX: store for Detail API lookup
    db.add(order)
    db.commit()

    return {"orderId": order_id, "paymentProcessUrl": payment_url, "upiLink": _payg.get("UpiLink")}


# ── Cancel Order ──────────────────────────────────────────────────────────────

def cancel_order(db: Session, order_id: int, is_refund: bool, reason: str, platform: str) -> bool:
    order = db.query(Orders).filter(
        Orders.orderId == order_id, Orders.deletedInd == False
    ).first()
    if not order:
        raise ValueError("Order not found.")

    new_state = "Refunded" if is_refund else "Cancelled"
    order.state    = new_state
    order.reason   = reason
    order.platform = platform
    order.modifiedDate = datetime.now(timezone.utc)

    db.add(OrderTrackingStatus(
        orderId=order_id,
        status=new_state,
        createdDate=datetime.now(timezone.utc)
    ))

    # Revert stock: increment ReturnedQuantity for each item in the order.
    # AvailableQuantity is recomputed in-Python: total - processed + refunded.
    try:
        from app.productvariantdetail.repository import increment_returned_quantity
        _now = datetime.now(timezone.utc)
        pvd_rows = db.execute(
            text("""
                SELECT "ProductVariantDetailId", SUM("Quantity")::INTEGER AS qty
                FROM   twam."OrderItems"
                WHERE  "OrderId"    = :oid
                  AND  "DeletedInd" = false
                  AND  "ProductVariantDetailId" IS NOT NULL
                GROUP BY "ProductVariantDetailId"
            """),
            {"oid": order_id}
        ).fetchall()
        for pvd_id_row, qty in pvd_rows:
            increment_returned_quantity(db, pvd_id_row, qty, now=_now)
    except Exception as _se:
        print(f"[Order] Stock revert failed for order {order_id}: {_se}")

    # TODO: Integrate IPaymentService.RefundPaymentAsync
    db.commit()
    
    # Send cancellation notification
    try:
        from app.ekart.notifications import send_order_cancelled_notification
        send_order_cancelled_notification(db, order_id, reason)
    except Exception as e:
        print(f"[Order] Cancel notification failed for order {order_id}: {e}")
    
    return True


# ── Return Order ──────────────────────────────────────────────────────────────

def create_return_order(db: Session, data) -> bool:
    try:
        return_order = Orders(
            orderNumber=generate_order_number(),
            referenceOrderId=data.orderId,
            deletedInd=False,
            isReturn=True,
            state="Return Initiated",
            createdDate=datetime.now(timezone.utc),
            personalId=0
        )
        db.add(return_order)
        db.flush()
        order_id = return_order.orderId

        db.add(OrderTrackingStatus(
            orderId=order_id,
            status="Return Initiated",
            createdDate=datetime.now(timezone.utc)
        ))

        for x in (data.orderItems or []):
            # ── BUG FIX: use PascalCase attribute names ──────────────────────
            db.add(OrderItems(
                OrderId              = order_id,
                OrderItemNumber      = generate_order_item_number(),
                Quantity             = x.quantity,
                Price                = x.price,
                ReferenceOrderItemId = x.orderItemId,
                IsReturn             = True,
                CreatedDate          = datetime.now(timezone.utc),
                DeletedInd           = False,
            ))

        db.add(OrderReturnInfo(
            orderId=order_id,
            reasonId=data.reasonId,
            reason=data.reason,
            otherReason=data.otherReason,
            refundChoice=data.shippingOption,
            createdDate=datetime.now(timezone.utc),
            deletedInd=False,
        ))

        db.commit()
        
        # Send return initiated notification
        try:
            from app.ekart.notifications import send_return_initiated_notification
            send_return_initiated_notification(db, data.orderId)  # Notify for original order
        except Exception as e:
            print(f"[Order] Return notification failed for order {order_id}: {e}")
        
        return True
    except Exception:
        db.rollback()
        return False


# ── Get Order List ────────────────────────────────────────────────────────────

def get_order_list(db: Session, filters, order_ascending, order_property, page_index, page_size) -> dict:
    query = db.query(Orders).filter(
        Orders.deletedInd == False,
        # Hide orders that were initiated but never paid for.
        # "AwaitingPayment" = payment page opened but gateway not yet confirmed.
        # "PaymentFailed"   = timed out or explicitly failed/expired by frontend.
        # Both are invisible to the user; only confirmed/processing/delivered etc. appear.
        Orders.state.notin_(["AwaitingPayment", "PaymentFailed"]),
    )

    days_value, remaining_filters = None, []
    if filters:
        for f in filters:
            if f.get("property") == "days":
                days_value = f.get("value")
            else:
                remaining_filters.append(f)

    from datetime import timedelta
    now = datetime.now()
    if days_value == "30":
        query = query.filter(Orders.orderDate >= now - timedelta(days=30))
    elif days_value == "180":
        query = query.filter(Orders.orderDate >= now - timedelta(days=180))
    elif days_value in ("1", "-1"):
        query = query.filter(Orders.orderDate != None)

    query = apply_filters(query, Orders, remaining_filters)
    query = apply_ordering(query, Orders, order_property, order_ascending)
    total = query.count()
    query = apply_pagination(query, page_index, page_size)
    orders = query.all()

    order_ids = [o.orderId for o in orders]
    items_map = {}
    for item in _get_order_items_with_details(db, order_ids, include_reviews=True):
        items_map.setdefault(item["orderId"], []).append(item)

    result = []
    for o in orders:
        tracking_base = get_setting("TRACKING_BASE_URL", "https://app.elite.ekartlogistics.in/track")
        tracking_url  = f"{tracking_base}/{o.trackingId}" if o.trackingId else None
        result.append({
            "orderId": o.orderId,
            "totalAmount": float(o.totalAmount) if o.totalAmount else None,
            "orderDate": o.orderDate,
            "userProfileId": o.userProfileId,
            "state": o.state,
            "orderNumber": o.orderNumber,
            "shippingAddressId": o.shippingAddressId,
            "paymentAccount": o.paymentAccount,
            "paymentTransactionRefNo": o.paymentTransactionRefNo,
            "couponAmount": float(o.couponAmount) if o.couponAmount else None,
            "deliveryCharge": float(o.deliveryCharge) if o.deliveryCharge else None,
            "taxAmount": float(o.taxAmount) if o.taxAmount else None,
            "subTotal": float(o.subTotal) if o.subTotal else None,
            "isShipped": o.isShipped,
            "reason": o.reason,
            "deliveredDate": o.deliveredDate,
            "trackingId": o.trackingId,
            "trackingUrl": tracking_url,
            "address": _get_address(db, o.shippingAddressId),
            "deliveryInfo": _get_delivery_info(db, o.shippingTypeId),
            "orderItems": items_map.get(o.orderId, []),
        })

    return build_paged_response(total, result)


# ── Get Admin Order List ──────────────────────────────────────────────────────

def get_admin_order_list(db: Session, filters, order_ascending, order_property,
                         page_index, page_size, start_date=None, end_date=None) -> dict:
    query = db.query(Orders).filter(Orders.deletedInd == False)

    if start_date and end_date:
        sd = datetime.fromisoformat(start_date)
        ed = datetime.fromisoformat(end_date)
        query = query.filter(Orders.orderDate >= sd, Orders.orderDate <= ed)

    query = apply_filters(query, Orders, filters)
    query = apply_ordering(query, Orders, order_property, order_ascending)
    total = query.count()
    query = apply_pagination(query, page_index, page_size)
    orders = query.all()

    ref_ids = [o.referenceOrderId for o in orders if o.referenceOrderId]
    ref_agent_map = {}
    if ref_ids:
        ref_orders = db.query(Orders).filter(Orders.orderId.in_(ref_ids)).all()
        ref_agent_map = {o.orderId: o.deliveryAgent for o in ref_orders}
        
    order_ids = [o.orderId for o in orders]
    print("DEBUG order_ids:", order_ids)
    items_map = {}
    for item in _get_order_items_with_details(db, order_ids):
        items_map.setdefault(item["orderId"], []).append(item)
    print("DEBUG items_map keys:", list(items_map.keys()))
    print(orders[0].__dict__)

    result = []
    for o in orders:
        order_items = items_map.get(o.orderId, [])
        total_items = len(order_items)
        total_qty   = sum(i.get("quantity") or 0 for i in order_items)
        for item in order_items:
            item["totalItems"]    = total_items
            item["totalQuantity"] = total_qty

        result.append({
            "orderId": o.orderId,
            "totalAmount": float(o.totalAmount) if o.totalAmount else None,
            "orderDate": o.orderDate,
            "userProfileId": o.userProfileId,
            "state": o.state,
            "orderNumber": o.orderNumber,
            "shippingAddressId": o.shippingAddressId,
            "paymentAccount": o.paymentAccount,
            "paymentTransactionRefNo": o.paymentTransactionRefNo,
            "couponAmount": float(o.couponAmount) if o.couponAmount else None,
            "deliveryCharge": float(o.deliveryCharge) if o.deliveryCharge else None,
            "taxAmount": float(o.taxAmount) if o.taxAmount else None,
            "subTotal": float(o.subTotal) if o.subTotal else None,
            "reason": o.reason,
            "deliveryAgent": o.deliveryAgent or ref_agent_map.get(o.referenceOrderId),
            "referenceOrderId": o.referenceOrderId,
            "address": _get_address(db, o.shippingAddressId),
            "orderItems": order_items,
        })

    return build_paged_response(total, result)


# ── Get Order Details ─────────────────────────────────────────────────────────

def get_order_details(db: Session, order_id: int) -> Optional[dict]:
    order = db.query(Orders).filter(
        Orders.orderId == order_id, Orders.deletedInd == False
    ).first()
    if not order:
        return None

    items = _get_order_items_with_details(db, [order_id])

    tracking_base = get_setting("TRACKING_BASE_URL", "https://app.elite.ekartlogistics.in/track")
    tracking_url  = f"{tracking_base}/{order.trackingId}" if order.trackingId else None

    return {
        "orderId":               order.orderId,
        "totalAmount":           float(order.totalAmount) if order.totalAmount else None,
        "orderDate":             order.orderDate,
        "userProfileId":         order.userProfileId,
        "state":                 order.state,
        "orderNumber":           order.orderNumber,
        "shippingAddressId":     order.shippingAddressId,
        "billingAddressId":      order.billingAddressId,
        "paymentAccount":        order.paymentAccount,
        "paymentTransactionRefNo": order.paymentTransactionRefNo,
        "paymentMethod":         order.paymentMethod,
        "paymentStatus":         None,
        "paymentReasonCode":     None,
        "subTotal":              float(order.subTotal) if order.subTotal else None,
        "taxAmount":             float(order.taxAmount) if order.taxAmount else None,
        "deliveryCharge":        float(order.deliveryCharge) if order.deliveryCharge else None,
        "couponAmount":          float(order.couponAmount) if order.couponAmount else None,
        # ── Shipping & tracking fields (used by profile tracking page) ────────
        "trackingId":            order.trackingId,
        "trackingUrl":           tracking_url,
        "isShipped":             order.isShipped or False,
        "deliveryAgent":         order.deliveryAgent,
        "deliveredDate":         order.deliveredDate,
        "shippingTypeId":        order.shippingTypeId,
        # ── Addresses ─────────────────────────────────────────────────────────
        "shippingAddress":       _get_address(db, order.shippingAddressId),
        "billingAddress":        _get_address(db, order.billingAddressId),
        "deliveryInfo":          _get_delivery_info(db, order.shippingTypeId),
        "orderItems":            items,
    }


# ── Order Tracking ────────────────────────────────────────────────────────────

def get_order_tracking(db: Session, order_id: int) -> dict:
    order = db.query(Orders).filter(Orders.orderId == order_id).first()
    if not order:
        return {"steps": [{"title": "Error", "description": "Order not found.", "status": "pending"}]}

    statuses   = db.query(OrderTrackingStatus).filter(OrderTrackingStatus.orderId == order_id).all()
    status_map = {s.status: s for s in statuses}

    def _fmt(key):
        s = status_map.get(key)
        return s.createdDate.strftime("%Y-%m-%d %H:%M:%S") if s and s.createdDate else None

    steps = []

    # ── Step 1: Payment / Order state ──────────────────────────────────────
    if "Pending" in status_map:
        steps.append({"title": "Order placed", "description": "Awaiting payment confirmation.",
                      "date": _fmt("Pending"), "status": "done"})

    if "Failed" in status_map or "Payment Failed" in status_map:
        key = "Payment Failed" if "Payment Failed" in status_map else "Failed"
        steps.append({"title": "Payment failed", "description": f"Order {order.orderNumber} payment failed.",
                      "date": _fmt(key), "status": "done"})

    if "Confirmed" in status_map:
        steps.append({"title": "Order confirmed", "description": "Payment received. Order confirmed.",
                      "date": _fmt("Confirmed"), "status": "done"})

    # ── Step 2: Pickup / Processing (Ekart picked up parcel) ──────────────
    # Ekart webhook writes both raw "Picked Up" and mapped "Processing" status.
    processing_date = _fmt("Processing") or _fmt("Picked Up") or _fmt("Out for Pickup")
    if processing_date or order.isShipped:
        tracking_base = get_setting("TRACKING_BASE_URL", "https://app.elite.ekartlogistics.in/track")
        tracking_url  = f"{tracking_base}/{order.trackingId}" if order.trackingId else None
        tracking_note = f"Tracking ID: {order.trackingId}" if order.trackingId else "Shipment created."
        steps.append({"title": "Shipment created", "description": tracking_note,
                      "trackingUrl": tracking_url,
                      "date": processing_date, "status": "done"})

    # ── Step 3: In transit / Shipped ───────────────────────────────────────
    shipped_date = _fmt("Shipped") or _fmt("In Transit")
    if shipped_date and order.isShipped:
        steps.append({"title": "In transit", "description": "Your order is on its way.",
                      "date": shipped_date, "status": "done"})

    # ── Step 4: Out for delivery ───────────────────────────────────────────
    ofd_date = _fmt("Out for Delivery") or _fmt("Out for delivery")
    if ofd_date:
        steps.append({"title": "Out for delivery", "description": "Order is out for delivery today.",
                      "date": ofd_date, "status": "done"})
    elif order.isShipped and order.state not in ("Delivered", "Cancelled", "Returned",
                                                  "Return Initiated", "Returned"):
        # Shipment created but not yet out — show as pending next step
        steps.append({"title": "Out for delivery", "description": "Your order is on its way.",
                      "date": None, "status": "active"})

    # ── Step 5: Delivered ──────────────────────────────────────────────────
    delivered_date = (
        order.deliveredDate.strftime("%Y-%m-%d %H:%M:%S") if order.deliveredDate
        else _fmt("Delivered")
    )
    if delivered_date or order.state == "Delivered":
        steps.append({"title": "Delivered", "description": "Your order has been delivered.",
                      "date": delivered_date, "status": "done"})

    # ── Return / Cancel ────────────────────────────────────────────────────
    if "Return Initiated" in status_map:
        steps.append({"title": "Return initiated", "description": "Return pickup scheduled.",
                      "date": _fmt("Return Initiated"), "status": "done"})
    if order.state == "Cancelled":
        steps.append({"title": "Order cancelled", "description": "Order was cancelled.",
                      "date": _fmt("Cancelled"), "status": "done"})
    if order.state == "Returned":
        steps.append({"title": "Order returned", "description": "Order returned successfully.",
                      "date": _fmt("Returned"), "status": "done"})
    refund = db.query(OrderRefund).filter(OrderRefund.orderId == order_id).first()
    if refund and refund.refundDateTime:
        steps.append({"title": "Refund Initiated", "description": "Refund initiated.",
                      "date": refund.refundDateTime.strftime("%Y-%m-%d %H:%M:%S"), "status": "done"})
        if refund.refundPaymentResponseCode in (0, 1):
            steps.append({
                "title": "Refunded",
                "description": f"{order.totalAmount} refunded to your original payment method.",
                "date": refund.updatedDateTime.strftime("%Y-%m-%d %H:%M:%S") if refund.updatedDateTime else None,
                "status": "done"
            })

    return {"steps": steps}


# ── Return Order Items ────────────────────────────────────────────────────────

def get_return_order_items(db: Session, order_id: int, filters, order_ascending,
                           order_property, page_index, page_size) -> dict:
    order = db.query(Orders).filter(
        Orders.orderId == order_id, Orders.deletedInd == False
    ).first()
    if not order:
        return None

    from datetime import timedelta
    return_expiry = (order.orderDate + timedelta(days=7)) if order.orderDate else None
    now = datetime.now(timezone.utc)

    sql = f"""
        SELECT
            oi."OrderItemId", oi."Quantity", oi."Price",
            p."Name", s."SizeLabel", pv."Color", pv."IsReturnAvailable",
            (SELECT pi2."FilePath"
             FROM twam."ProductImage" pi2
             WHERE pi2."ProductVariantId" = oi."ProductVariantId" AND pi2."DeletedInd" = false
             LIMIT 1) AS product_image
        FROM twam."OrderItems" oi
        LEFT JOIN twam."Products" p ON p."ProductId" = oi."ProductId"
        LEFT JOIN twam."ProductVariants" pv ON pv."ProductVariantId" = oi."ProductVariantId"
        LEFT JOIN twam."ProductVariantDetail" pvd ON pvd."ProductVariantDetailId" = oi."ProductVariantDetailId"
        LEFT JOIN mdm."Size" s ON s."SizeId" = pvd."Size"
        WHERE oi."OrderId" = :order_id AND oi."DeletedInd" = false AND pv."IsReturnAvailable" = true
    """
    rows = db.execute(text(sql), {"order_id": order_id}).fetchall()

    items = [{
        "orderItemId": r[0], "quantity": r[1],
        "price": float(r[2]) if r[2] else None,
        "productName": r[3], "size": r[4], "color": r[5],
        "isReturnable": r[6] or False,
        "productImage": resolve_url(r[7]) or "",
        "returnExpiryDate": return_expiry,
        "isReturnExpired": (now > return_expiry) if return_expiry else None,
    } for r in rows]

    return {
        "count": 1,
        "list": [{
            "orderId": order.orderId,
            "orderNumber": order.orderNumber,
            "orderDate": order.deliveredDate or order.orderDate,
            "orderItems": items
        }],
        "parameters": None
    }


# ── Update Order Status (Admin) ───────────────────────────────────────────────

def update_order_status(
    db: Session, 
    order_id: int, 
    new_state: str,
    tracking_id: str = None,
    delivery_agent: str = None,
) -> bool:
    """
    Update order status and send appropriate notifications.
    Used by admin panel and Ekart webhooks.
    """
    order = db.query(Orders).filter(
        Orders.orderId == order_id,
        Orders.deletedInd == False
    ).first()
    
    if not order:
        return False
    
    old_state = order.state
    order.state = new_state
    order.modifiedDate = datetime.now(timezone.utc)
    
    # Update tracking info if provided
    if tracking_id:
        order.trackingId = tracking_id
        order.isShipped = True
    
    if delivery_agent:
        order.deliveryAgent = delivery_agent
    
    # Set delivered date
    if new_state == "Delivered" and not order.deliveredDate:
        order.deliveredDate = datetime.now(timezone.utc)
    
    # Add tracking status record
    db.add(OrderTrackingStatus(
        orderId=order_id,
        status=new_state,
        createdDate=datetime.now(timezone.utc)
    ))
    
    db.commit()
    
    # Send notifications based on state change
    if old_state != new_state:
        try:
            from app.ekart.notifications import send_order_status_notification
            send_order_status_notification(db, order_id, new_state)
        except Exception as e:
            print(f"[Order] Status notification failed for order {order_id}: {e}")
    
    return True