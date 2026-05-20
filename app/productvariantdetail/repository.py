from datetime import datetime, timezone
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import asc, desc, text
from app.shared.filters import apply_filters, apply_ordering, apply_pagination, build_paged_response

from .models import ProductVariantDetail

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
        "productCode":            pvd.productCode,
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
        "isAlphabetSize":         pvd.isAlphabetSize,
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
    total_qty = data.stockQuantity or 0

    pvd = ProductVariantDetail(
        productId         = data.productId,
        productVariantId  = data.productVariantId,
        productCode       = data.productCode,
        sizeId            = data.sizeId,
        stockQuantity     = total_qty,
        processedQuantity = 0,       # always starts at 0
        returnedQuantity  = 0,       # always starts at 0
        availableQuantity = total_qty,  # available = total at creation
        discountPercent   = data.discountPercent,
        mrpPrice          = data.mrpPrice,
        finalPrice        = data.finalPrice,
        taxAmount         = data.taxAmount,
        cgst              = data.cgst,
        sgst              = data.sgst,
        userProfileId     = data.userProfileId,
        state             = data.state,
        cupSizeId         = data.cupSizeId,
        stockId           = data.stockId,
        isAlphabetSize    = data.isAlphabetSize,
        isLowStock        = 0 < total_qty <= 3,
        isOutOfStock      = total_qty <= 0,
        createdBy         = data.createdBy,
        createdDate       = datetime.now(timezone.utc),
        deletedInd        = False,
    )
    db.add(pvd)
    db.commit()
    db.refresh(pvd)

    try:
        db.execute(
            text("SELECT twam.\"AutoCreateProductAudit\"(:ref_id, :profile)"),
            {"ref_id": pvd.productVariantDetailId, "profile": "ProductVariantDetail"}
        )
        db.commit()
    except Exception:
        pass

    return pvd.productVariantDetailId


# ── Update ────────────────────────────────────────────────────────────────────

def update_variant_detail(db: Session, pvd_id: int, data) -> Optional[int]:
    """
    Stock update rules:
    - stockQuantity  (total)     → admin can change this (restocking).
    - processedQuantity          → set by orders; admin CAN override if needed.
    - returnedQuantity           → set by refunds; admin CAN override if needed.
    - availableQuantity          → ALWAYS auto-derived: total - processed + refunded.
    Price / discount / tax fields are updated normally.
    """
    pvd = db.query(ProductVariantDetail).filter(
        ProductVariantDetail.productVariantDetailId == pvd_id
    ).first()
    if not pvd:
        return None

    # Price / meta fields — apply only when provided
    simple_fields = {
        "productCode":     data.productCode,
        "sizeId":          data.sizeId,
        "discountPercent": data.discountPercent,
        "mrpPrice":        data.mrpPrice,
        "finalPrice":      data.finalPrice,
        "taxAmount":       data.taxAmount,
        "cgst":            data.cgst,
        "sgst":            data.sgst,
        "state":           data.state,
        "cupSizeId":       data.cupSizeId,
        "isAlphabetSize":  data.isAlphabetSize,
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
        pass

    return pvd_id


# ── Dedicated stock-only update (called by order / refund flows) ──────────────

def increment_processed_quantity(db: Session, pvd_id: int, qty: int, now=None) -> bool:
    """
    Increment ProcessedQuantity by `qty` (order placed / payment confirmed).
    Recomputes AvailableQuantity in place.
    Returns True on success.
    """
    pvd = db.query(ProductVariantDetail).filter(
        ProductVariantDetail.productVariantDetailId == pvd_id
    ).first()
    if not pvd:
        return False
    pvd.processedQuantity = (pvd.processedQuantity or 0) + qty
    _apply_stock_formula(pvd)
    pvd.modifiedDate = now or datetime.now(timezone.utc)
    return True


def increment_returned_quantity(db: Session, pvd_id: int, qty: int, now=None) -> bool:
    """
    Increment ReturnedQuantity by `qty` (refund / cancellation).
    Recomputes AvailableQuantity in place.
    Returns True on success.
    """
    pvd = db.query(ProductVariantDetail).filter(
        ProductVariantDetail.productVariantDetailId == pvd_id
    ).first()
    if not pvd:
        return False
    pvd.returnedQuantity = (pvd.returnedQuantity or 0) + qty
    _apply_stock_formula(pvd)
    pvd.modifiedDate = now or datetime.now(timezone.utc)
    return True


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