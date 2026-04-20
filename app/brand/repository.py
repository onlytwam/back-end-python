from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import asc, desc
from sqlalchemy.orm import Session

from app.shared.filters import apply_filters
from .models import Brand

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Safe column map — only columns that actually
# exist on the Brand model
# ─────────────────────────────────────────────

_ORDERABLE_COLUMNS: dict[str, Any] = {
    "brandid":          Brand.brandId,
    "id":               Brand.brandId,
    "brandname":        Brand.brandName,
    "name":             Brand.brandName,        # frontend sends "Name"
    "branddescription": Brand.brandDescription,
    "description":      Brand.brandDescription,
    "brandlogo":        Brand.brandLogo,
    "logo":             Brand.brandLogo,
    "isactive":         Brand.isActive,
    "createddate":      Brand.createdDate,
    "modifieddate":     Brand.modifiedDate,
}


def _safe_order(query, order_property: Optional[str], order_ascending: Optional[bool]):
    """
    Apply ordering only when the column key is recognised.
    Falls back to brandName ASC so the query never crashes.
    """
    col = _ORDERABLE_COLUMNS.get((order_property or "").lower())
    if col is None:
        logger.warning(
            "Unknown order_property %r — falling back to brandName ASC", order_property
        )
        col = Brand.brandName

    direction = asc if (order_ascending is None or order_ascending) else desc
    return query.order_by(direction(col))


# ─────────────────────────────────────────────
# Read
# ─────────────────────────────────────────────
def get_all_brands(
    db: Session,
    filters: Optional[list[dict]] = None,
    order_property: Optional[str] = None,
    order_ascending: Optional[bool] = None,
    page_index: Optional[int] = 1,
    page_size: Optional[int] = 20,
) -> dict[str, Any]:

    #  Base query (exclude deleted)
    query = db.query(Brand).filter(
        (Brand.deletedInd.is_(False)) | (Brand.deletedInd.is_(None))
    )

    #  Apply filters safely
    try:
        query = apply_filters(query, Brand, filters)
    except Exception as exc:
        logger.error("apply_filters failed: %s — skipping filters", exc)

    #  Apply ordering
    query = _safe_order(query, order_property, order_ascending)

    #  Total count BEFORE pagination
    total = query.count()
    logger.info(f"get_all_brands | total after filters = {total}")

    #  Safe pagination
    if page_index and page_size:
        page_index = max(page_index, 1)
        page_size = max(page_size, 1)
        query = query.offset((page_index - 1) * page_size).limit(page_size)

    #  Fetch data
    rows = query.all()
    logger.info(f"get_all_brands | returning {len(rows)} rows")

    #  Final response
    return {
        "list": rows,
        "count": total
    }
def get_brand_by_id(db: Session, brand_id: int) -> Brand | None:
    return (
        db.query(Brand)
        .filter(
            Brand.brandId == brand_id,
            (Brand.deletedInd == False) | (Brand.deletedInd == None),  # noqa: E712
        )
        .first()
    )


# ─────────────────────────────────────────────
# Write
# ─────────────────────────────────────────────

def create_brand(db: Session, data: dict[str, Any]) -> Brand:
    data.setdefault("isActive", True)
    data.setdefault("deletedInd", False)
    brand = Brand(**data)
    db.add(brand)
    db.commit()
    db.refresh(brand)
    return brand


def update_brand(db: Session, brand_id: int, data: dict[str, Any]) -> Brand | None:
    brand = get_brand_by_id(db, brand_id)
    if not brand:
        return None

    for field, value in data.items():
        if value is not None:
            setattr(brand, field, value)

    brand.isActive = brand.isActive if brand.isActive is not None else True
    db.commit()
    db.refresh(brand)
    return brand


def delete_brand(db: Session, brand_id: int) -> bool:
    brand = get_brand_by_id(db, brand_id)
    if not brand:
        return False

    # Soft delete — set deletedInd = True instead of removing the row
    brand.deletedInd = True
    db.commit()
    return True