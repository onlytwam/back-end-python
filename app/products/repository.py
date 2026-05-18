from datetime import datetime, timezone
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import asc, desc, text
from app.shared.filters import apply_filters, apply_ordering, apply_pagination, build_paged_response

from .models import Products


def _to_response(db: Session, product: Products) -> dict:
    """Attach brandName + brand visibility flags, and categoryName from joins."""
    brand_name       = None
    brand_is_active  = None
    brand_deleted    = None

    if product.brandId:
        row = db.execute(
            text(
                'SELECT "brandName", "isActive", "deletedInd" '
                'FROM mdm."Brand" WHERE "brandId" = :id LIMIT 1'
            ),
            {"id": product.brandId},
        ).fetchone()
        if row:
            brand_name      = row[0]
            brand_is_active = bool(row[1]) if row[1] is not None else True
            brand_deleted   = bool(row[2]) if row[2] is not None else False

    # Resolve category name, active status, and deleted status.
    # NOTE: Do NOT filter by DeletedInd here — we need to detect deleted
    # categories so the admin UI can show "Inactive – Category deleted".
    category_name       = None
    category_is_active  = None
    category_deleted    = None
    cat_id = product.childCategoryId or product.categoryId
    if cat_id:
        row = db.execute(
            text(
                'SELECT "Name", "IsActive", "DeletedInd" FROM twam."Category" '
                'WHERE "CategoryId" = :id LIMIT 1'
            ),
            {"id": cat_id},
        ).fetchone()
        if row:
            category_name      = row[0]
            category_is_active = bool(row[1]) if row[1] is not None else True
            category_deleted   = bool(row[2]) if row[2] is not None else False
            # A deleted category always counts as inactive for product status
            if category_deleted:
                category_is_active = False

    return {
        "productId":        product.productId,
        "productCode":      product.productCode,
        "name":             product.name,
        "description":      product.description,
        "categoryId":       product.categoryId,
        "childCategoryId":  product.childCategoryId,
        "categoryName":        category_name,
        "categoryIsActive":    category_is_active,
        "categoryDeletedInd":  category_deleted,
        "brandId":          product.brandId,
        "brandName":        brand_name,
        # These two fields let the admin UI show warning badges
        "brandIsActive":    brand_is_active,
        "brandDeletedInd":  brand_deleted,
        "personalId":       product.personalId,
        "userProfileId":    product.userProfileId,
        "tag":              product.tag,
        "state":            product.state,
        "createdBy":        product.createdBy,
        "modifiedBy":       product.modifiedBy,
        "createdDate":      product.createdDate,
        "modifiedDate":     product.modifiedDate,
    }


def _active_brand_ids(db: Session) -> list[int]:
    """Return brandIds that are active AND not deleted — used for public filtering."""
    rows = db.execute(
        text(
            'SELECT "brandId" FROM mdm."Brand" '
            'WHERE "deletedInd" = false AND "isActive" = true'
        )
    ).fetchall()
    return [r[0] for r in rows]


def _active_category_ids(db: Session) -> list[int]:
    """Return categoryIds that are active AND not deleted — used for public filtering."""
    rows = db.execute(
        text(
            'SELECT "CategoryId" FROM twam."Category" '
            'WHERE "DeletedInd" = false AND "IsActive" = true'
        )
    ).fetchall()
    return [r[0] for r in rows]


def get_all_products(
    db: Session,
    filters: Optional[List[dict]] = None,
    order_ascending: Optional[bool] = None,
    order_property: Optional[str] = None,
    page_index: Optional[int] = None,
    page_size: Optional[int] = None,
    public_only: bool = True,   # False = admin view (shows all products regardless of brand/category status)
) -> dict:
    query = db.query(Products).filter(Products.deletedInd == False)

    # ── Visibility gates (public storefront only) ──────────────────────────────
    # Hide products whose brand OR category has been deactivated / deleted.
    # Null brand / null category are always shown (no gate to block them).
    if public_only:
        from sqlalchemy import or_

        # Brand gate: exclude products whose brand is hidden or deleted
        active_brand_ids = _active_brand_ids(db)
        query = query.filter(
            or_(
                Products.brandId == None,                    # no brand → always show
                Products.brandId.in_(active_brand_ids),      # brand is active & not deleted
            )
        )

        # Category gate: exclude products whose category is inactive or deleted
        active_cat_ids = _active_category_ids(db)
        query = query.filter(
            or_(
                Products.childCategoryId == None,            # prefer child category …
                Products.childCategoryId.in_(active_cat_ids),
            )
        ).filter(
            or_(
                Products.categoryId == None,
                Products.categoryId.in_(active_cat_ids),     # … fallback to parent category
            )
        )

    query = apply_filters(query, Products, filters)
    query = apply_ordering(query, Products, order_property, order_ascending)

    total = query.count()

    if page_index and page_size:
        query = query.offset((page_index - 1) * page_size).limit(page_size)

    rows = query.all()
    return {
        "count":      total,
        "list":       [_to_response(db, r) for r in rows],
        "parameters": None,
    }


def get_product_by_id(db: Session, product_id: int) -> Optional[dict]:
    product = db.query(Products).filter(
        Products.productId == product_id,
        Products.deletedInd == False,
    ).first()
    if not product:
        return None
    return _to_response(db, product)


def create_product(db: Session, data) -> int:
    product = Products(
        productCode     = data.productCode,
        name            = data.name,
        description     = data.description,
        categoryId      = data.categoryId,
        childCategoryId = data.childCategoryId,
        brandId         = data.brandId,
        personalId      = data.personalId,
        userProfileId   = data.userProfileId,
        state           = data.state,
        tag             = data.tag,
        createdBy       = data.createdBy,
        createdDate     = datetime.now(timezone.utc),
        deletedInd      = False,
    )
    db.add(product)
    db.commit()
    db.refresh(product)

    try:
        db.execute(
            text("SELECT twam.\"AutoCreateProductAudit\"(:ref_id, :profile)"),
            {"ref_id": product.productId, "profile": "Product"}
        )
        db.commit()
    except Exception:
        pass

    return product.productId


def update_product(db: Session, product_id: int, data) -> Optional[int]:
    product = db.query(Products).filter(
        Products.productId == product_id,
        Products.deletedInd == False,
    ).first()
    if not product:
        return None

    for field in ("productCode", "name", "description", "categoryId",
                  "childCategoryId", "brandId", "personalId",
                  "userProfileId", "state", "tag", "modifiedBy"):
        val = getattr(data, field, None)
        if val is not None:
            setattr(product, field, val)

    product.modifiedDate = datetime.now(timezone.utc)
    db.commit()

    try:
        db.execute(
            text("SELECT twam.\"AutoCreateProductAudit\"(:ref_id, :profile)"),
            {"ref_id": product_id, "profile": "Product"}
        )
        db.commit()
    except Exception:
        pass

    return product_id


def delete_product(db: Session, product_id: int) -> bool:
    product = db.query(Products).filter(
        Products.productId == product_id,
        Products.deletedInd == False,
    ).first()
    if not product:
        return False

    product.deletedInd   = True
    product.modifiedDate = datetime.now(timezone.utc)
    db.commit()

    try:
        db.execute(text(
            'UPDATE twam."ProductVariants" SET "DeletedInd" = true WHERE "ProductId" = :pid'
        ), {"pid": product_id})
        db.execute(text(
            'UPDATE twam."ProductVariantDetail" SET "DeletedInd" = true WHERE "ProductId" = :pid'
        ), {"pid": product_id})
        db.execute(text(
            'UPDATE twam."ProductImage" SET "DeletedInd" = true WHERE "ProductId" = :pid'
        ), {"pid": product_id})
        db.commit()
    except Exception:
        pass

    return True


def check_product_validations(db: Session, product_id: int, state: Optional[str] = None) -> bool:
    if state == "Returned For Correction":
        count = db.execute(
            text("""
                SELECT COUNT(*) FROM twam."ProductVariantDetail"
                WHERE "DeletedInd" = false
                  AND "ProductId" = :pid
                  AND "ProcessedQuantity" > 0
            """),
            {"pid": product_id},
        ).scalar()
        return (count or 0) > 0

    variant_count = db.execute(
        text('SELECT COUNT(*) FROM twam."ProductVariants" WHERE "DeletedInd" = false AND "ProductId" = :pid'),
        {"pid": product_id},
    ).scalar()
    detail_count = db.execute(
        text('SELECT COUNT(*) FROM twam."ProductVariantDetail" WHERE "DeletedInd" = false AND "ProductId" = :pid'),
        {"pid": product_id},
    ).scalar()
    return (variant_count or 0) > 0 and (detail_count or 0) > 0