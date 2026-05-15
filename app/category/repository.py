from datetime import datetime, timezone
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import asc, desc, text
from app.shared.filters import apply_filters, apply_ordering, apply_pagination, build_paged_response

from .models import Category


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
) -> dict:
    # Admin grid: show ALL non-deleted categories including inactive ones.
    # Public/user-facing callers pass with_products_only=True which adds its
    # own isActive filter via the expanded-product-count logic below.
    query = db.query(Category).filter(Category.deletedInd == False)
    if with_products_only:
        # Public endpoints only show active categories
        query = query.filter(Category.isActive == True)

    if only_child:
        query = query.filter(Category.groupCategoryId != True)

    # ── Filter to only categories (and their parents) that have products ──────
    if with_products_only:
        counts_raw = get_category_product_counts(db)
        cats_with_products: set = {row["categoryId"] for row in counts_raw if row["count"] > 0}

        # Expand upward: mark every ancestor of a populated child as included
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
            # No products at all — return empty result set
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
            "categoryImage":      r.categoryImage,
            "createdDate":        r.createdDate,
            "modifiedDate":       r.modifiedDate,
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
        "categoryImage":    cat.categoryImage,
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
        categoryImage    = data.categoryImage,
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
    # Only overwrite the stored image URL when the caller sends an explicit
    # non-None value.  None means "field was not supplied" — leave DB intact.
    if data.categoryImage is not None:
        cat.categoryImage = data.categoryImage
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

    # If hiding → always cascade hide to all children
    if not new_state:
        for child in children:
            child.isActive     = False
            child.modifiedDate = datetime.now(timezone.utc)
    # If un-hiding and caller opted in → restore children too
    elif unhide_children:
        for child in children:
            child.isActive     = True
            child.modifiedDate = datetime.now(timezone.utc)

    db.commit()
    return {
        "categoryId":  category_id,
        "isActive":    new_state,
        "childCount":  child_count,
        "isParent":    child_count > 0,
    }


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

    # Build the set of category IDs that have at least one approved product.
    # A parent category must have products in itself OR in any descendant to be shown.
    counts_raw = get_category_product_counts(db)
    categories_with_products: set = {row["categoryId"] for row in counts_raw if row["count"] > 0}

    # Expand upward: if a child has products, mark every ancestor as having products too.
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
            # Skip this entire branch if it has no products
            if child.categoryId not in expanded:
                continue
            node = {
                "categoryId":       child.categoryId,
                "name":             child.name,
                "url":              child.url,
                "about":            child.about,
                "categoryImage":    child.categoryImage,
                "parentCategoryId": child.parentCategoryId,
                "groupCategoryId":  child.groupCategoryId,
                "children":         _build(child.categoryId),
            }
            result.append(node)
        return result

    return {"categories": _build(None)}


# ── Category product counts ───────────────────────────────────────────────────

def get_category_product_counts(db: Session) -> List[dict]:
    # Join the Category table so we only count products whose category is
    # still alive (DeletedInd=false) AND visible (IsActive=true).
    # Without this join, deleting or hiding a category would still make its
    # product counts appear in the menu/storefront.
    child_sql = text("""
        SELECT p."ChildCategoryId" AS category_id, COUNT(*) AS cnt
        FROM twam."ProductVariantDetail" pvd
        JOIN twam."Products"  p  ON p."ProductId"   = pvd."ProductId"
        JOIN twam."Category"  cc ON cc."CategoryId" = p."ChildCategoryId"
        WHERE pvd."DeletedInd" = false
          AND p."DeletedInd"   = false
          AND pvd."State"      = 'Approved'
          AND p."ChildCategoryId" IS NOT NULL
          AND cc."DeletedInd"  = false
          AND cc."IsActive"    = true
        GROUP BY p."ChildCategoryId"
    """)
    parent_sql = text("""
        SELECT p."CategoryId" AS category_id, COUNT(*) AS cnt
        FROM twam."ProductVariantDetail" pvd
        JOIN twam."Products"  p  ON p."ProductId"  = pvd."ProductId"
        JOIN twam."Category"  pc ON pc."CategoryId" = p."CategoryId"
        WHERE pvd."DeletedInd" = false
          AND p."DeletedInd"   = false
          AND pvd."State"      = 'Approved'
          AND p."CategoryId"   IS NOT NULL
          AND pc."DeletedInd"  = false
          AND pc."IsActive"    = true
        GROUP BY p."CategoryId"
    """)
    rows  = db.execute(child_sql).fetchall()
    rows += db.execute(parent_sql).fetchall()
    return [{"categoryId": r[0], "count": r[1]} for r in rows if r[0]]