from datetime import datetime, timezone
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import asc, desc, text, func
import sqlalchemy
from app.shared.filters import apply_filters, apply_ordering, apply_pagination, build_paged_response

from .models import ProductVariantDetail
from app.stocklog.models import StockLog


def _auto_compute_tax(db: Session, product_id: int, final_price: float) -> dict:
    """
    Resolve CGST/SGST for a variant detail by looking up the parent product's
    TaxHSNCode and matching the correct price slab.

    Returns {"cgst": rate%, "sgst": rate%, "taxAmount": Rupees} or zeros if
    the product has no HSN code assigned.
    """
    try:
        from app.taxhsncode.repository import compute_tax_for_product
        result = compute_tax_for_product(db, product_id, final_price)
        return {
            "cgst":      result["cgstRate"],
            "sgst":      result["sgstRate"],
            "taxAmount": result["taxAmount"],
        }
    except Exception:
        return {"cgst": 0.0, "sgst": 0.0, "taxAmount": 0.0}

# ── Stock formula helper ──────────────────────────────────────────────────────

def _apply_stock_formula(pvd: ProductVariantDetail) -> None:
    """
    Single source-of-truth for stock maths:
        available = total - processed + refunded
    Clamp to 0. Also update isOutOfStock / isLowStock flags.
    Called after every create / update that touches stock fields.
    """
    total     = pvd.stockQuantity     or 0
    processed = pvd.processedQuantity or 0
    refunded  = pvd.returnedQuantity  or 0
    available = max(0, total - processed + refunded)
    pvd.availableQuantity = available
    pvd.isOutOfStock = available <= 0
    pvd.isLowStock   = 0 < available <= 3


# ── Helpers ───────────────────────────────────────────────────────────────────

def _enrich(db: Session, pvd: ProductVariantDetail) -> dict:
    """Add sizeLabel and cupSizeLabel via JOIN."""
    size_label     = None
    cup_size_label = None

    if pvd.sizeId:
        r = db.execute(
            text('SELECT "SizeLabel" FROM mdm."Size" WHERE "SizeId"=:id LIMIT 1'),
            {"id": pvd.sizeId}
        ).fetchone()
        size_label = r[0] if r else None

    if pvd.cupSizeId:
        r = db.execute(
            text('SELECT "SizeLabel" FROM mdm."CupSize" WHERE "CupSizeId"=:id LIMIT 1'),
            {"id": pvd.cupSizeId}
        ).fetchone()
        cup_size_label = r[0] if r else None

    return {
        "productVariantDetailId": pvd.productVariantDetailId,
        "productId":              pvd.productId,
        "productVariantId":       pvd.productVariantId,
        "sizeId":                 pvd.sizeId,
        "sizeLabel":              size_label,
        "stockQuantity":          pvd.stockQuantity,
        "processedQuantity":      pvd.processedQuantity,
        "returnedQuantity":       pvd.returnedQuantity,
        "availableQuantity":      pvd.availableQuantity,
        "amendmentQuantity":      pvd.amendmentQuantity,
        "discountPercent":        pvd.discountPercent,
        "mrpPrice":               float(pvd.mrpPrice)   if pvd.mrpPrice   else None,
        "finalPrice":             float(pvd.finalPrice)  if pvd.finalPrice  else None,
        "taxAmount":              float(pvd.taxAmount)   if pvd.taxAmount   else None,
        "cgst":                   float(pvd.cgst)        if pvd.cgst        else None,
        "sgst":                   float(pvd.sgst)        if pvd.sgst        else None,
        "userProfileId":          pvd.userProfileId,
        "state":                  pvd.state,
        "stockId":                pvd.stockId,
        "cupSizeId":              pvd.cupSizeId,
        "cupSizeLabel":           cup_size_label,
        "isLowStock":             pvd.isLowStock,
        "isOutOfStock":           pvd.isOutOfStock,
        "createdDate":            pvd.createdDate,
        "modifiedDate":           pvd.modifiedDate,
        "deletedInd":             pvd.deletedInd,
        "isActive":               pvd.isActive if pvd.isActive is not None else True,
    }


# ── Grid / List ───────────────────────────────────────────────────────────────

def get_all_variant_details(
    db: Session,
    filters=None,
    order_ascending=True,
    order_property=None,
    page_index=1,
    page_size=100
):
    query = db.query(ProductVariantDetail)

    if filters:
        for f in filters:
            prop  = f.get("property")
            value = f.get("value")

            if prop in ["ProductVariantId", "productVariantId"]:
                query = query.filter(
                    ProductVariantDetail.productVariantId == int(value)
                )

            if prop in ["ProductId", "productId"]:
                query = query.filter(
                    ProductVariantDetail.productId == int(value)
                )

    if page_index and page_size:
        query = query.offset((page_index - 1) * page_size).limit(page_size)

    results = query.all()
    return {"list": results, "count": len(results)}


# ── Get by ID ─────────────────────────────────────────────────────────────────

def get_variant_detail_by_id(db: Session, pvd_id: int) -> Optional[dict]:
    pvd = db.query(ProductVariantDetail).filter(
        ProductVariantDetail.productVariantDetailId == pvd_id,
        ProductVariantDetail.deletedInd == False
    ).first()
    if not pvd:
        return None
    return _enrich(db, pvd)


# ── Create ────────────────────────────────────────────────────────────────────

def create_variant_detail(db: Session, data) -> int:
    """
    When a variant detail is created the admin-supplied stockQuantity becomes
    the total quantity.  processedQuantity and returnedQuantity start at 0.
    availableQuantity is derived: total - processed + refunded.
    """
    total_qty   = data.stockQuantity or 0
    # Default finalPrice (selling price) to mrpPrice when not provided
    mrp_price   = float(data.mrpPrice or 0)
    final_price = float(data.finalPrice) if data.finalPrice else mrp_price

    # Auto-compute CGST/SGST/TaxAmount from the parent product's TaxHSNCode + price slab.
    tax_info = _auto_compute_tax(db, data.productId, final_price)

    # ── Satisfy chk_store_xor_warehouse DB CHECK constraint ─────────────────
    # The DB requires exactly one of StoreId / WarehouseId to be non-null.
    # Both are optional in the API; when neither is supplied we default to
    # warehouseId=1 (the primary warehouse) so the constraint is satisfied.
    _store_id     = getattr(data, "storeId",     None)
    _warehouse_id = getattr(data, "warehouseId", None)
    if _store_id is None and _warehouse_id is None:
        _warehouse_id = 1  # default warehouse — satisfies chk_store_xor_warehouse

    pvd = ProductVariantDetail(
        productId         = data.productId,
        productVariantId  = data.productVariantId,
        sizeId            = data.sizeId,
        stockQuantity     = total_qty,
        processedQuantity = 0,
        returnedQuantity  = 0,
        availableQuantity = total_qty,
        discountPercent   = data.discountPercent,
        mrpPrice          = mrp_price or None,
        finalPrice        = final_price,
        taxAmount         = tax_info["taxAmount"],
        cgst              = tax_info["cgst"],
        sgst              = tax_info["sgst"],
        userProfileId     = data.userProfileId,
        state             = data.state,
        cupSizeId         = data.cupSizeId,
        storeId           = _store_id,
        warehouseId       = _warehouse_id,
        stockId           = data.stockId,
        isLowStock        = 0 < total_qty <= 3,
        isOutOfStock      = total_qty <= 0,
        createdBy         = data.createdBy,
        createdDate       = datetime.now(timezone.utc),
        isActive          = True,
        deletedInd        = False,
    )
    try:
        db.add(pvd)
        db.commit()
        db.refresh(pvd)
    except Exception as exc:
        db.rollback()
        raise exc  # re-raise so router returns HTTP 500 with the real DB error message

    # ── Snapshot the ID immediately after the first successful commit ──────────
    # This must happen BEFORE any subsequent try/except blocks that may rollback,
    # because a rollback expires all ORM object attributes (pvd becomes detached).
    pvd_id_to_return = pvd.productVariantDetailId
    pvd_product_id = pvd.productId
    pvd_variant_id = pvd.productVariantId
    pvd_stock_id   = pvd.stockId
    pvd_stock_qty  = pvd.stockQuantity or 0
    pvd_processed  = pvd.processedQuantity or 0
    pvd_returned   = pvd.returnedQuantity or 0
    pvd_available  = pvd.availableQuantity or 0
    pvd_created_by = getattr(data, "createdBy", None) or "system"

    # ── Optional: call AutoCreateProductAudit SP (non-fatal if missing) ───────
    try:
        db.execute(
            text("SELECT twam.\"AutoCreateProductAudit\"(:ref_id, :profile)"),
            {"ref_id": pvd_id_to_return, "profile": "ProductVariantDetail"}
        )
        db.commit()
    except Exception:
        db.rollback()  # MUST rollback — PostgreSQL aborts the txn on any error,
                       # leaving the session in InFailedSqlTransaction state which
                       # causes all subsequent db calls in this session to also fail.

    # ── Auto-create initial StockLog entry (system-only, not shown in history UI) ──
    try:
        log_entry = StockLog(
            productVariantDetailId = pvd_id_to_return,
            productId              = pvd_product_id,
            productVariantId       = pvd_variant_id,
            stockId                = pvd_stock_id,
            changeType             = "INITIAL_STOCK",
            previousValue          = None,
            newValue               = pvd_stock_qty,
            totalStock             = pvd_stock_qty,
            processedQuantity      = pvd_processed,
            returnedQuantity       = pvd_returned,
            availableQuantity      = pvd_available,
            note                   = "Created with product variant detail",
            changedBy              = pvd_created_by,
        )
        db.add(log_entry)
        db.commit()
    except Exception:
        db.rollback()  # Clean up failed transaction — session must be valid before we return

    return pvd_id_to_return


# ── Update ────────────────────────────────────────────────────────────────────

def update_variant_detail(db: Session, pvd_id: int, data) -> Optional[int]:
    """
    Stock update rules:
    - stockQuantity  (total)     → admin can change this (restocking).
    - processedQuantity          → set by orders; admin CAN override if needed.
    - returnedQuantity           → set by refunds; admin CAN override if needed.
    - availableQuantity          → ALWAYS auto-derived: total - processed + refunded.
    Price / discount / tax fields are updated normally.

    NOTE: Stock changes made here are logged with changedBy="ProductDetail" so
    they can be distinguished from Stock Page changes (changedBy="Admin") in
    the stock log. Stock management should be done via the Stock Page.
    """
    pvd = db.query(ProductVariantDetail).filter(
        ProductVariantDetail.productVariantDetailId == pvd_id
    ).first()
    if not pvd:
        return None

    # Snapshot BEFORE changes for stock log diff
    prev_stock     = pvd.stockQuantity     or 0
    prev_processed = pvd.processedQuantity or 0
    prev_returned  = pvd.returnedQuantity  or 0

    # Auto-compute CGST/SGST from TaxHSNCode + new price
    new_final_price = float(data.finalPrice if data.finalPrice is not None else pvd.finalPrice or 0)
    tax_info = _auto_compute_tax(db, pvd.productId, new_final_price)

    # Price / meta fields — apply only when provided
    simple_fields = {
        "sizeId":          data.sizeId,
        "discountPercent": data.discountPercent,
        "mrpPrice":        data.mrpPrice,
        "finalPrice":      new_final_price,
        "taxAmount":       tax_info["taxAmount"],
        "cgst":            tax_info["cgst"],
        "sgst":            tax_info["sgst"],
        "state":           data.state,
        "stockId":         data.stockId,
        "cupSizeId":       data.cupSizeId,
        "amendmentQuantity": data.amendmentQuantity,
        "modifiedBy":      data.modifiedBy,
    }
    for field, val in simple_fields.items():
        if val is not None:
            setattr(pvd, field, val)

    # Stock fields — update raw values then recompute available
    if data.stockQuantity is not None:
        pvd.stockQuantity = data.stockQuantity
    if data.processedQuantity is not None:
        pvd.processedQuantity = data.processedQuantity
    if data.returnedQuantity is not None:
        pvd.returnedQuantity = data.returnedQuantity

    # ✅ Always recompute available = total - processed + refunded
    _apply_stock_formula(pvd)

    pvd.modifiedDate = datetime.now(timezone.utc)
    db.commit()

    # Audit
    try:
        db.execute(
            text("SELECT twam.\"AutoCreateProductAudit\"(:ref_id, :profile)"),
            {"ref_id": pvd_id, "profile": "ProductVariantDetail"}
        )
        db.commit()
    except Exception:
        db.rollback()  # Rollback aborted txn so subsequent StockLog inserts succeed

    # ── Auto-create StockLog entries for any changed stock fields ────────────
    # changedBy = "ProductDetail" so these are distinguishable in the stock log.
    try:
        stock_changes = []
        if data.stockQuantity is not None and data.stockQuantity != prev_stock:
            stock_changes.append(("TOTAL_STOCK", prev_stock, pvd.stockQuantity or 0))
        if data.processedQuantity is not None and data.processedQuantity != prev_processed:
            stock_changes.append(("PROCESSED", prev_processed, pvd.processedQuantity or 0))
        if data.returnedQuantity is not None and data.returnedQuantity != prev_returned:
            stock_changes.append(("RETURNED", prev_returned, pvd.returnedQuantity or 0))

        for change_type, prev_val, new_val in stock_changes:
            log_entry = StockLog(
                productVariantDetailId = pvd_id,
                productId              = pvd.productId,
                productVariantId       = pvd.productVariantId,
                stockId                = pvd.stockId,
                changeType             = change_type,
                previousValue          = prev_val,
                newValue               = new_val,
                totalStock             = pvd.stockQuantity or 0,
                processedQuantity      = pvd.processedQuantity or 0,
                returnedQuantity       = pvd.returnedQuantity or 0,
                availableQuantity      = pvd.availableQuantity or 0,
                note                   = "Stock changed via Product Detail page",
                changedBy              = "ProductDetail",
            )
            db.add(log_entry)
        if stock_changes:
            db.commit()
    except Exception:
        pass  # Never block product update if stock log fails

    return pvd_id


# ── Dedicated stock-only update (called by order / refund flows) ──────────────

def increment_processed_quantity(db: Session, pvd_id: int, qty: int, now=None,
                                  order_id: Optional[int] = None) -> bool:
    """
    Increment ProcessedQuantity by `qty` (order placed / payment confirmed).
    Recomputes AvailableQuantity in place.
    Creates a StockLog entry with changeType=ORDER_PROCESSED, changedBy=order_system.
    Returns True on success.
    """
    pvd = db.query(ProductVariantDetail).filter(
        ProductVariantDetail.productVariantDetailId == pvd_id
    ).first()
    if not pvd:
        return False

    prev_processed = pvd.processedQuantity or 0
    pvd.processedQuantity = prev_processed + qty
    _apply_stock_formula(pvd)
    pvd.modifiedDate = now or datetime.now(timezone.utc)

    # ── Log this order-driven processed change ────────────────────────────────
    try:
        note = f"Order processed — quantity +{qty}"
        if order_id:
            note = f"Order #{order_id} processed — quantity +{qty}"
        log_entry = StockLog(
            productVariantDetailId = pvd_id,
            productId              = pvd.productId,
            productVariantId       = pvd.productVariantId,
            stockId                = pvd.stockId,
            changeType             = "ORDER_PROCESSED",
            previousValue          = prev_processed,
            newValue               = pvd.processedQuantity,
            totalStock             = pvd.stockQuantity or 0,
            processedQuantity      = pvd.processedQuantity,
            returnedQuantity       = pvd.returnedQuantity or 0,
            availableQuantity      = pvd.availableQuantity,
            note                   = note,
            changedBy              = "order_system",
        )
        db.add(log_entry)
    except Exception:
        pass  # Never block order flow if stock log fails

    return True


def increment_returned_quantity(db: Session, pvd_id: int, qty: int, now=None,
                                 order_id: Optional[int] = None) -> bool:
    """
    Increment ReturnedQuantity by `qty` (refund / cancellation).
    Recomputes AvailableQuantity in place.
    Creates a StockLog entry with changeType=ORDER_RETURNED, changedBy=order_system.
    Returns True on success.
    """
    pvd = db.query(ProductVariantDetail).filter(
        ProductVariantDetail.productVariantDetailId == pvd_id
    ).first()
    if not pvd:
        return False

    prev_returned = pvd.returnedQuantity or 0
    pvd.returnedQuantity = prev_returned + qty
    _apply_stock_formula(pvd)
    pvd.modifiedDate = now or datetime.now(timezone.utc)

    # ── Log this order-driven returned change ─────────────────────────────────
    try:
        note = f"Order returned/cancelled — quantity +{qty}"
        if order_id:
            note = f"Order #{order_id} returned/cancelled — quantity +{qty}"
        log_entry = StockLog(
            productVariantDetailId = pvd_id,
            productId              = pvd.productId,
            productVariantId       = pvd.productVariantId,
            stockId                = pvd.stockId,
            changeType             = "ORDER_RETURNED",
            previousValue          = prev_returned,
            newValue               = pvd.returnedQuantity,
            totalStock             = pvd.stockQuantity or 0,
            processedQuantity      = pvd.processedQuantity or 0,
            returnedQuantity       = pvd.returnedQuantity,
            availableQuantity      = pvd.availableQuantity,
            note                   = note,
            changedBy              = "order_system",
        )
        db.add(log_entry)
    except Exception:
        pass  # Never block order flow if stock log fails

    return True


# ── Find by Stock ID ──────────────────────────────────────────────────────────

def find_by_stock_id(db: Session, stock_id: str) -> Optional[dict]:
    """
    Case-insensitive lookup of a ProductVariantDetail by StockId.
    StockId is stored as TEXT — no CAST needed.
    Returns None when no match is found.
    """
    try:
        row = db.execute(
            text("""
                SELECT
                    "ProductVariantDetailId",
                    "ProductId",
                    "ProductVariantId",
                    "StockId",
                    "Size",
                    "CupSize",
                    "StockQuantity",
                    "AvailableQuantity"
                FROM twam."ProductVariantDetail"
                WHERE "DeletedInd" = false
                  AND LOWER("StockId") = LOWER(:sid)
                LIMIT 1
            """),
            {"sid": stock_id.strip()},
        ).fetchone()
    except Exception:
        db.rollback()
        return None

    if not row:
        return None

    pvd_detail_id = row[0]
    product_id    = row[1]
    variant_id    = row[2]
    stock_id_val  = row[3]
    size_id       = row[4]   # int FK stored in "Size" column
    cup_size_id   = row[5]   # int FK stored in "CupSize" column
    stock_qty     = row[6]
    avail_qty     = row[7]

    size_label = None
    try:
        if size_id:
            r = db.execute(
                text('SELECT "SizeLabel" FROM mdm."Size" WHERE "SizeId" = :id LIMIT 1'),
                {"id": size_id},
            ).fetchone()
            size_label = r[0] if r else None
    except Exception:
        db.rollback()

    cup_size_label = None
    try:
        if cup_size_id:
            r = db.execute(
                text('SELECT "SizeLabel" FROM mdm."CupSize" WHERE "CupSizeId" = :id LIMIT 1'),
                {"id": cup_size_id},
            ).fetchone()
            cup_size_label = r[0] if r else None
    except Exception:
        db.rollback()

    return {
        "found":                  True,
        "productId":              product_id,
        "productVariantId":       variant_id,
        "productVariantDetailId": pvd_detail_id,
        "sizeId":                 size_id,
        "sizeLabel":              size_label,
        "cupSizeId":              cup_size_id,
        "cupSizeLabel":           cup_size_label,
        "stockId":                stock_id_val,
        "stockQuantity":          stock_qty,
        "availableQuantity":      avail_qty,
    }

# ── Delete ────────────────────────────────────────────────────────────────────

def delete_variant_detail(db: Session, detail_id: int):
    try:
        vd = db.query(ProductVariantDetail).filter(
            ProductVariantDetail.productVariantDetailId == detail_id
        ).first()
        if not vd:
            return False
        db.delete(vd)
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        raise e


# ── Duplicate Check ───────────────────────────────────────────────────────────

def check_duplicate(db: Session, product_id: int, product_variant_id: int,
                    size_id: Optional[int], cup_size_id: Optional[int],
                    current_id: int = 0) -> int:
    query = db.query(ProductVariantDetail).filter(
        ProductVariantDetail.productId        == product_id,
        ProductVariantDetail.productVariantId == product_variant_id,
        ProductVariantDetail.sizeId           == size_id,
        ProductVariantDetail.cupSizeId        == cup_size_id,
        ProductVariantDetail.deletedInd       == False
    )
    if current_id > 0:
        query = query.filter(ProductVariantDetail.productVariantDetailId != current_id)
    return query.count()


# ── User product detail ───────────────────────────────────────────────────────

def get_user_product_detail(db: Session, pvd_id: int) -> Optional[dict]:
    pvd = db.query(ProductVariantDetail).filter(
        ProductVariantDetail.productVariantDetailId == pvd_id,
        ProductVariantDetail.deletedInd == False
    ).first()
    if not pvd:
        return None
    return {
        "productVariantDetailId": pvd.productVariantDetailId,
        "productId":              pvd.productId,
        "productVariantId":       pvd.productVariantId,
        "price":                  float(pvd.finalPrice)      if pvd.finalPrice      else None,
        "originalPrice":          float(pvd.mrpPrice)        if pvd.mrpPrice        else None,
        "discountPercentage":     float(pvd.discountPercent) if pvd.discountPercent else None,
        # Expose available (not total) stock to the storefront
        "stockQuantity":          pvd.availableQuantity,
        "processedQuantity":      pvd.processedQuantity,
    }

# ── Admin: full size palette for a variant ────────────────────────────────────

def get_admin_size_palette(
    db: Session,
    product_variant_id: int,
    is_alpha_override: Optional[bool] = None,
    is_cup_override:   Optional[bool] = None,
) -> dict:
    """
    Returns every MDM band size and cup size together with their PVD data
    for the given variant. Used by the admin product detail size palette.

    is_alpha_override / is_cup_override: when provided (from the live UI
    checkbox state), these take priority over the saved DB value so sizes
    update immediately on toggle without requiring a Save first.
    """
    from app.productvariant.models import ProductVariant  # local import to avoid circular

    # ── Fetch variant to get isAlphaSize / isCupSize flags ───────────────────
    variant_row = db.query(ProductVariant).filter(
        ProductVariant.productVariantId == product_variant_id
    ).first()

    # Live override from the UI checkbox takes priority over the saved DB value.
    # isAlphaSize: None/False → numerical (default); True → alphabetical.
    if is_alpha_override is not None:
        is_alpha_size = is_alpha_override
    else:
        is_alpha_size = variant_row.isAlphaSize is True if variant_row else False

    if is_cup_override is not None:
        isCupSize = is_cup_override
    else:
        isCupSize = bool(variant_row.isCupSize) if variant_row else False

    # ── Fetch all PVDs for this variant ──────────────────────────────────────
    pvds = db.query(ProductVariantDetail).filter(
        ProductVariantDetail.productVariantId == product_variant_id,
        ProductVariantDetail.deletedInd == False,
    ).all()

    variant_size_ids = {p.sizeId for p in pvds if p.sizeId}
    variant_cup_ids  = {p.cupSizeId for p in pvds if p.cupSizeId}

    # ── Load MDM band sizes filtered by variant.isAlphaSize ─────────────────
    # True  → alphabetical (XS/S/M/L…); False/None → numerical (28/30/32…)
    _stype = "alphabetical" if is_alpha_size else "numerical"
    all_band_rows = db.execute(text(f"""
        SELECT "SizeId", "SizeLabel", "SizeCode", "SizeType", "OrderNo"
        FROM mdm."Size"
        WHERE ("DeletedInd" = false OR "DeletedInd" IS NULL)
          AND ("IsActive" = true OR "IsActive" IS NULL)
          AND ("IsCupSize" IS NULL OR "IsCupSize" = false)
          AND "SizeType" = '{_stype}'
        ORDER BY "OrderNo" NULLS LAST, "SizeId"
    """)).fetchall()

    # ── Load MDM cup sizes only when variant.isCupSize = True ────────────────
    all_cup_rows = []
    if isCupSize:
        all_cup_rows = db.execute(text("""
            SELECT "CupSizeId", "SizeLabel"
            FROM mdm."CupSize"
            WHERE ("DeletedInd" = false OR "DeletedInd" IS NULL)
              AND ("IsActive" = true OR "IsActive" IS NULL)
            ORDER BY "CupSizeId"
        """)).fetchall()

    # ── Index PVDs by sizeId and cupSizeId ───────────────────────────────────
    def _pvd_dict(p: ProductVariantDetail) -> dict:
        total     = p.stockQuantity     or 0
        processed = p.processedQuantity or 0
        refunded  = p.returnedQuantity  or 0
        available = p.availableQuantity if p.availableQuantity is not None \
                    else max(0, total - processed + refunded)
        return {
            "productVariantDetailId": p.productVariantDetailId,
            "productId":              p.productId,
            "productVariantId":       p.productVariantId,
            "sizeId":                 p.sizeId,
            "cupSizeId":              p.cupSizeId,
            "stockId":                p.stockId,          # ← was missing
            "stockQuantity":          total,
            "processedQuantity":      processed,
            "returnedQuantity":       refunded,
            "availableQuantity":      available,
            "mrpPrice":               float(p.mrpPrice)        if p.mrpPrice        else 0,
            "discountPercent":        float(p.discountPercent) if p.discountPercent else 0,
            "finalPrice":             float(p.finalPrice)      if p.finalPrice      else 0,
            "cgst":                   float(p.cgst)            if p.cgst            else 0,
            "sgst":                   float(p.sgst)            if p.sgst            else 0,
            "isActive":               p.isActive,
        }

    # Index PVDs for band sizes — include even when cupSizeId is also set
    # (bra PVDs have both sizeId=band AND cupSizeId=cup, e.g. 34D)
    pvd_by_size_list: dict[int, list] = {}
    pvd_by_cup: dict[int, list] = {}
    for p in pvds:
        if p.sizeId:
            pvd_by_size_list.setdefault(p.sizeId, []).append(_pvd_dict(p))
        if p.cupSizeId:
            pvd_by_cup.setdefault(p.cupSizeId, []).append(_pvd_dict(p))

    # Aggregate stock across all cup-size combos for each band size
    pvd_by_size: dict[int, dict] = {}
    for size_id, pvd_list in pvd_by_size_list.items():
        if len(pvd_list) == 1:
            pvd_by_size[size_id] = pvd_list[0]
        else:
            base = dict(pvd_list[0])
            base["stockQuantity"]    = sum(p["stockQuantity"]    for p in pvd_list)
            base["processedQuantity"]= sum(p["processedQuantity"] for p in pvd_list)
            base["returnedQuantity"] = sum(p["returnedQuantity"]  for p in pvd_list)
            base["availableQuantity"]= sum(p["availableQuantity"] for p in pvd_list)
            base["stockId"]          = None   # stockId is per cup-combination, not per band
            base["isAggregate"]      = True
            base["pvdCount"]         = len(pvd_list)
            base["pvds"]             = pvd_list  # pass all individual pvds so cup tiles can read their own stockId
            pvd_by_size[size_id]     = base

    # ── Build response ────────────────────────────────────────────────────────
    band_sizes = [
        {
            "sizeId":    r[0],
            "sizeLabel": r[1],
            "sizeCode":  r[2],
            "sizeType":  r[3],
            "orderNo":   r[4],
            "inVariant": r[0] in variant_size_ids,
            "pvd":       pvd_by_size.get(r[0]),
        }
        for r in all_band_rows
    ]

    cup_sizes = [
        {
            "cupSizeId": r[0],
            "sizeLabel": r[1],
            "inVariant": r[0] in variant_cup_ids,
            "pvds":      pvd_by_cup.get(r[0], []),
        }
        for r in all_cup_rows
    ]

    # ── Group band sizes by SizeType for easier frontend rendering ───────────
    from collections import defaultdict
    band_sizes_by_type: dict = defaultdict(list)
    for s in band_sizes:
        key = s["sizeType"] or "other"
        band_sizes_by_type[key].append(s)

    return {
        "productVariantId": product_variant_id,
        "isCupSize":        isCupSize,
        "isAlphaSize":      is_alpha_size,
        "bandSizes":        band_sizes,
        "bandSizesByType":  dict(band_sizes_by_type),
        "cupSizes":         cup_sizes,
    }