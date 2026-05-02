from __future__ import annotations

from typing import Any, Optional
from sqlalchemy.orm import Session

from app.shared.filters import apply_filters, apply_ordering
from .models import Brand


# ─────────────────────────────────────────────
# Read
# ─────────────────────────────────────────────

def get_all_brands(
    db:              Session,
    filters:         Optional[list[dict]] = None,
    order_property:  Optional[str]        = None,
    order_ascending: Optional[bool]       = None,
    page_index:      Optional[int]        = None,
    page_size:       Optional[int]        = None,
) -> dict[str, Any]:
    query = db.query(Brand)
    query = apply_filters(query, Brand, filters)
    query = apply_ordering(query, Brand, order_property, order_ascending)

    # Count is taken after filters but before pagination — intentional
    total = query.count()

    if page_index is not None and page_size is not None:
        query = query.offset((page_index - 1) * page_size).limit(page_size)

    return {"list": query.all(), "count": total}


def get_brand_by_id(db: Session, brand_id: int) -> Brand | None:
    return db.query(Brand).filter(Brand.brandId == brand_id).first()


# ─────────────────────────────────────────────
# Write
# ─────────────────────────────────────────────

def create_brand(db: Session, data: dict[str, Any]) -> Brand:
    data.setdefault("isActive", True)

    # Only pass keys that exist as columns on the model
    valid_keys = {c.key for c in Brand.__table__.columns}
    filtered_data = {k: v for k, v in data.items() if k in valid_keys}

    brand = Brand(**filtered_data)
    db.add(brand)
    db.commit()
    db.refresh(brand)
    return brand


def update_brand(db: Session, brand_id: int, data: dict[str, Any]) -> Brand | None:
    brand = db.query(Brand).filter(Brand.brandId == brand_id).first()
    if not brand:
        return None

    # Only update keys that are valid columns on the model
    valid_keys = {c.key for c in Brand.__table__.columns}
    for key, value in data.items():
        if key in valid_keys:
            setattr(brand, key, value)

    db.commit()
    db.refresh(brand)
    return brand


def delete_brand(db: Session, brand_id: int) -> bool:
    brand = db.query(Brand).filter(Brand.brandId == brand_id).first()
    if not brand:
        return False

    db.delete(brand)
    db.commit()
    return True