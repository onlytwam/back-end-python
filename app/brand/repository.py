from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from sqlalchemy.orm import Session
from sqlalchemy import exists

from app.shared.filters import apply_filters, apply_ordering
from .models import Brand
from app.products.models import Products


# ─────────────────────────────────────────────
# Read
# ─────────────────────────────────────────────

def get_all_brands(
    db:                Session,
    filters:           Optional[list[dict]] = None,
    order_property:    Optional[str]        = None,
    order_ascending:   Optional[bool]       = None,
    page_index:        Optional[int]        = None,
    page_size:         Optional[int]        = None,
    active_only:       bool                 = False,   # True → public (hide inactive/deleted)
    with_products_only: bool                = False,   # True → exclude brands with 0 live products
) -> dict[str, Any]:
    query = db.query(Brand).filter(Brand.deletedInd == False)

    # Public view: only active brands
    if active_only:
        query = query.filter(Brand.isActive == True)

    # Brands page: only show brands that have at least one active, non-deleted product.
    # We intentionally do NOT filter by product.state — the storefront only gates on
    # isActive + deletedInd, so a brand whose product is in "Draft"/"Created" state but
    # isActive=True should still appear on the brands page.
    if with_products_only:
        has_live_product = exists().where(
            Products.brandId    == Brand.brandId,
            Products.deletedInd == False,
            Products.isActive   == True,
        )
        query = query.filter(has_live_product)

    query = apply_filters(query, Brand, filters)
    query = apply_ordering(query, Brand, order_property, order_ascending)

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
    data.setdefault("deletedInd", False)

    valid_keys = {c.key for c in Brand.__table__.columns}
    filtered_data = {k: v for k, v in data.items() if k in valid_keys}

    brand = Brand(**filtered_data)
    db.add(brand)
    db.commit()
    db.refresh(brand)
    return brand


def update_brand(db: Session, brand_id: int, data: dict[str, Any]) -> Brand | None:
    brand = db.query(Brand).filter(
        Brand.brandId  == brand_id,
        Brand.deletedInd == False,
    ).first()
    if not brand:
        return None

    valid_keys = {c.key for c in Brand.__table__.columns}
    for key, value in data.items():
        if key in valid_keys:
            setattr(brand, key, value)

    brand.modifiedDate = datetime.now(timezone.utc)
    db.commit()
    db.refresh(brand)
    return brand


def toggle_brand_visibility(db: Session, brand_id: int) -> Brand | None:
    """Flip isActive (hide <-> show). Returns updated brand or None if not found/deleted."""
    brand = db.query(Brand).filter(
        Brand.brandId    == brand_id,
        Brand.deletedInd == False,
    ).first()
    if not brand:
        return None

    brand.isActive     = not bool(brand.isActive)
    brand.modifiedDate = datetime.now(timezone.utc)
    db.commit()
    db.refresh(brand)
    return brand


def delete_brand(db: Session, brand_id: int) -> bool:
    """Soft-delete: sets deletedInd=True so the brand is invisible to users
    but the record (and FK links on products) is preserved for admin view.
    """
    brand = db.query(Brand).filter(Brand.brandId == brand_id).first()
    if not brand:
        return False

    brand.deletedInd   = True
    brand.isActive     = False          # mark inactive too for safety
    brand.modifiedDate = datetime.now(timezone.utc)
    db.commit()
    return True