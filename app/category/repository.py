from datetime import datetime, timezone
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import asc, text
from app.shared.filters import apply_filters, apply_ordering, apply_pagination, build_paged_response

from .models import Category
from app.shared.url_helpers import resolve_url as _to_url, storage_path_for_db



# ── Admin grid ────────────────────────────────────────────────────────────────

def get_all_categories(
    db: Session,
    filters: Optional[List[dict]] = None,
    order_ascending: Optional[bool] = None,
    order_property: Optional[str] = None,
    page_index: Optional[int] = None,
    page_size: Optional[int] = None,
    only_child: bool = False,
    with_products_only: bool = False,
    include_deleted: bool = False,
) -> dict:
    if include_deleted:
        query = db.query(Category)
    else:
        query = db.query(Category).filter(Category.deletedInd == False)
    if with_products_only:
        query = query.filter(Category.isActive == True)

    if only_child:
        query = query.filter(Category.groupCategoryId != True)

    if with_products_only:
        counts_raw = get_category_product_counts(db)
        cats_with_products: set = {row["categoryId"] for row in counts_raw if row["count"] > 0}

        all_cats = db.query(Category).filter(Category.deletedInd == False).all()
        cat_map  = {c.categoryId: c for c in all_cats}
        expanded: set = set(cats_with_products)
        for cat_id in list(cats_with_products):
            cat = cat_map.get(cat_id)
            while cat and cat.parentCategoryId:
                parent = cat_map.get(cat.parentCategoryId)
                if parent:
                    expanded.add(parent.categoryId)
                cat = parent

        if expanded:
            query = query.filter(Category.categoryId.in_(expanded))
        else:
            return build_paged_response(0, [])

    query = apply_filters(query, Category, filters)
    if order_property:
        query = apply_ordering(query, Category, order_property, order_ascending)
    else:
        query = query.order_by(asc(Category.displayOrder))

    total = query.count()
    if page_index and page_size:
        query = query.offset((page_index - 1) * page_size).limit(page_size)

    rows = query.all()

    parent_ids = list({r.parentCategoryId for r in rows if r.parentCategoryId})
    parent_map = {}
    if parent_ids:
        parents = db.query(Category).filter(Category.categoryId.in_(parent_ids)).all()
        parent_map = {p.categoryId: p.name for p in parents}

    result = []
    for r in rows:
        result.append({
            "categoryId":         r.categoryId,
            "name":               r.name,
            "description":        r.description,
            "about":              r.about,
            "url":                r.url,
            "displayOrder":       r.displayOrder,
            "parentCategoryId":   r.parentCategoryId,
            "parentCategoryName": parent_map.get(r.parentCategoryId, ""),
            "groupCategoryId":    r.groupCategoryId,
            "isActive":           r.isActive,
            "categoryImage":      _to_url(r.categoryImage),
            "mobileBannerImage":  _to_url(r.mobileBannerImage) if hasattr(r, "mobileBannerImage") else None,
            "createdDate":        r.createdDate,
            "modifiedDate":       r.modifiedDate,
            "deletedInd":         bool(r.deletedInd) if r.deletedInd is not None else False,
        })
    return build_paged_response(total, result)


def get_category_by_id(db: Session, category_id: int) -> Optional[dict]:
    cat = db.query(Category).filter(
        Category.categoryId == category_id,
        Category.deletedInd == False
    ).first()
    if not cat:
        return None
    return {
        "categoryId":       cat.categoryId,
        "name":             cat.name,
        "description":      cat.description,
        "about":            cat.about,
        "url":              cat.url,
        "displayOrder":     cat.displayOrder,
        "parentCategoryId": cat.parentCategoryId,
        "groupCategoryId":  cat.groupCategoryId,
        "isActive":         cat.isActive,
        "categoryImage":    _to_url(cat.categoryImage),
        "mobileBannerImage": _to_url(cat.mobileBannerImage) if hasattr(cat, "mobileBannerImage") else None,
        "createdDate":      cat.createdDate,
        "modifiedDate":     cat.modifiedDate,
    }


def create_category(db: Session, data) -> int:
    auto_group = data.parentCategoryId is None

    cat = Category(
        name             = data.name,
        description      = data.description,
        about            = data.about,
        url              = data.url,
        displayOrder     = data.displayOrder,
        parentCategoryId = data.parentCategoryId,
        groupCategoryId  = data.groupCategoryId if data.groupCategoryId is not None else auto_group,
        isActive         = data.isActive if data.isActive is not None else True,
        categoryImage    = storage_path_for_db(data.categoryImage),
        mobileBannerImage = storage_path_for_db(data.mobileBannerImage) if getattr(data, "mobileBannerImage", None) else None,
        createdDate      = datetime.now(timezone.utc),
        deletedInd       = False,
    )
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return cat.categoryId


def update_category(db: Session, category_id: int, data) -> Optional[int]:
    cat = db.query(Category).filter(
        Category.categoryId == category_id,
        Category.deletedInd == False
    ).first()
    if not cat:
        return None

    cat.name             = data.name
    cat.description      = data.description
    cat.about            = data.about
    cat.url              = data.url
    cat.displayOrder     = data.displayOrder
    cat.parentCategoryId = data.parentCategoryId
    cat.isActive         = data.isActive if data.isActive is not None else True
    if data.categoryImage is not None:
        cat.categoryImage = storage_path_for_db(data.categoryImage)
    if getattr(data, 'mobileBannerImage', None) is not None:
        cat.mobileBannerImage = storage_path_for_db(data.mobileBannerImage)
    cat.modifiedDate     = datetime.now(timezone.utc)

    if data.groupCategoryId is not None:
        cat.groupCategoryId = data.groupCategoryId
    else:
        cat.groupCategoryId = data.parentCategoryId is None

    db.commit()
    return cat.categoryId


def delete_category(db: Session, category_id: int, cascade_children: bool = True):
    cat = db.query(Category).filter(
        Category.categoryId == category_id,
        Category.deletedInd == False
    ).first()
    if not cat:
        return None

    is_parent = cat.parentCategoryId is None or bool(cat.groupCategoryId)

    now = datetime.now(timezone.utc)
    cat.deletedInd   = True
    cat.modifiedDate = now

    children_deleted = 0
    if cascade_children:
        children = db.query(Category).filter(
            Category.parentCategoryId == category_id,
            Category.deletedInd       == False,
        ).all()
        for child in children:
            child.deletedInd   = True
            child.modifiedDate = now
            children_deleted  += 1

    db.commit()
    return {"categoryId": category_id, "isParent": is_parent, "childrenDeleted": children_deleted}


def get_children(db: Session, category_id: int) -> list:
    """Return all direct child categories of a given category."""
    return db.query(Category).filter(
        Category.parentCategoryId == category_id,
        Category.deletedInd       == False,
    ).all()


def toggle_category_visibility(
    db: Session,
    category_id: int,
    unhide_children: bool = False,
) -> Optional[dict]:
    """Toggle isActive (visible/hidden) for a category.

    Hiding a parent also hides all its children so the entire branch
    disappears from the storefront in one click.

    When un-hiding (new_state=True) and unhide_children=True, all children
    are restored to visible as well. If unhide_children=False the children
    remain hidden and the admin can manually restore them.
    """
    cat = db.query(Category).filter(
        Category.categoryId == category_id,
        Category.deletedInd == False
    ).first()
    if not cat:
        return None

    new_state = not bool(cat.isActive)
    cat.isActive     = new_state
    cat.modifiedDate = datetime.now(timezone.utc)

    children = db.query(Category).filter(
        Category.parentCategoryId == category_id,
        Category.deletedInd       == False,
    ).all()

    child_count = len(children)

    if not new_state:
        for child in children:
            child.isActive     = False
            child.modifiedDate = datetime.now(timezone.utc)
    elif unhide_children:
        for child in children:
            child.isActive     = True
            child.modifiedDate = datetime.now(timezone.utc)

    db.commit()
    return {
        "categoryId": category_id,
        "isActive":   new_state,
        "childCount": child_count,
        "isParent":   child_count > 0,
    }



def delete_mobile_banner_image(db: Session, category_id: int) -> Optional[str]:
    """Clear the mobileBannerImage field and return the old URL for blob deletion."""
    cat = db.query(Category).filter(
        Category.categoryId == category_id,
        Category.deletedInd == False
    ).first()
    if not cat:
        return None
    old_url = cat.mobileBannerImage
    cat.mobileBannerImage = None
    cat.modifiedDate = datetime.now(timezone.utc)
    db.commit()
    return old_url

def delete_category_image(db: Session, category_id: int) -> Optional[str]:
    """Clear the categoryImage field and return the old URL for blob deletion."""
    cat = db.query(Category).filter(
        Category.categoryId == category_id,
        Category.deletedInd == False
    ).first()
    if not cat:
        return None
    old_url = cat.categoryImage
    cat.categoryImage = None
    cat.modifiedDate  = datetime.now(timezone.utc)
    db.commit()
    return old_url


# ── User-facing: nested category menu tree ────────────────────────────────────

def get_category_menu(db: Session) -> dict:
    categories = db.query(Category).filter(
        Category.deletedInd == False,
        Category.isActive   == True
    ).order_by(
        asc(Category.parentCategoryId),
        asc(Category.displayOrder)
    ).all()

    counts_raw = get_category_product_counts(db)
    categories_with_products: set = {row["categoryId"] for row in counts_raw if row["count"] > 0}

    cat_map = {c.categoryId: c for c in categories}
    expanded: set = set(categories_with_products)
    for cat_id in list(categories_with_products):
        cat = cat_map.get(cat_id)
        while cat and cat.parentCategoryId:
            parent = cat_map.get(cat.parentCategoryId)
            if parent:
                expanded.add(parent.categoryId)
            cat = parent

    def _build(parent_id):
        result = []
        children = [c for c in categories if c.parentCategoryId == parent_id]
        for child in children:
            if child.categoryId not in expanded:
                continue
            node = {
                "categoryId":       child.categoryId,
                "name":             child.name,
                "url":              child.url,
                "about":            child.about,
                "categoryImage":    _to_url(child.categoryImage),
                "mobileBannerImage": _to_url(child.mobileBannerImage) if hasattr(child, "mobileBannerImage") else None,
                "parentCategoryId": child.parentCategoryId,
                "groupCategoryId":  child.groupCategoryId,
                "children":         _build(child.categoryId),
            }
            result.append(node)
        return result

    return {"categories": _build(None)}


# ── Category product counts ───────────────────────────────────────────────────

def get_category_product_counts(db: Session) -> List[dict]:
    # ── Count only products that are truly visible to storefront users ────────
    #
    # A product is invisible when ANY of these hold:
    #   • its brand  is hidden  (mdm.Brand.isActive  = false OR deletedInd = true)
    #   • its category is hidden (twam.Category.IsActive = false OR DeletedInd = true)
    #   • the product itself is deleted / not approved
    #
    # Brand gate (added here):
    #   LEFT JOIN mdm."Brand" b ON b."brandId" = p."BrandId"
    #   AND (p."BrandId" IS NULL OR (b."isActive" = true AND b."deletedInd" = false))
    #
    # Products with no brand (BrandId IS NULL) are always counted — the gate
    # only blocks products whose brand record exists but is inactive/deleted.
    #
    # This ensures that hiding a brand causes its products to be excluded from
    # the category counts, which in turn causes the now-empty categories to
    # disappear from the navbar and "Shop by Category" page — matching the
    # same visibility logic applied in products/repository.py get_all_products().
    sql = text("""
        SELECT category_id, SUM(cnt) AS total
        FROM (
            -- Products filed under a child category (ChildCategoryId)
            SELECT p."ChildCategoryId" AS category_id, COUNT(DISTINCT p."ProductId") AS cnt
            FROM twam."Products"  p
            JOIN twam."Category"  cc ON cc."CategoryId" = p."ChildCategoryId"
            LEFT JOIN mdm."Brand" b  ON b."brandId"     = p."BrandId"
            WHERE p."DeletedInd"          = false
              AND p."IsActive"            = true
              AND LOWER(p."State")        = 'approved'
              AND p."ChildCategoryId"     IS NOT NULL
              AND cc."DeletedInd"         = false
              AND cc."IsActive"           = true
              -- Brand gate: products with no brand are always visible;
              -- products whose brand is hidden/deleted are excluded.
              AND (
                    p."BrandId" IS NULL
                    OR (b."isActive" = true AND b."deletedInd" = false)
              )
            GROUP BY p."ChildCategoryId"

            UNION ALL

            -- Products filed directly under a parent category only (no child category assigned).
            -- The ChildCategoryId IS NULL guard is critical: without it, products that have
            -- both a CategoryId AND a ChildCategoryId would be counted here AND in the
            -- ChildCategoryId branch above, causing double-counting on the parent.
            SELECT p."CategoryId" AS category_id, COUNT(DISTINCT p."ProductId") AS cnt
            FROM twam."Products"  p
            JOIN twam."Category"  pc ON pc."CategoryId" = p."CategoryId"
            LEFT JOIN mdm."Brand" b  ON b."brandId"     = p."BrandId"
            WHERE p."DeletedInd"          = false
              AND p."IsActive"            = true
              AND LOWER(p."State")        = 'approved'
              AND p."CategoryId"          IS NOT NULL
              AND p."ChildCategoryId"     IS NULL
              AND pc."DeletedInd"         = false
              AND pc."IsActive"           = true
              -- Brand gate: same as above
              AND (
                    p."BrandId" IS NULL
                    OR (b."isActive" = true AND b."deletedInd" = false)
              )
            GROUP BY p."CategoryId"
        ) combined
        GROUP BY category_id
    """)
    rows = db.execute(sql).fetchall()
    return [{"categoryId": r[0], "count": int(r[1])} for r in rows if r[0]]