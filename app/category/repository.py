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
) -> dict:
    query = db.query(Category).filter(Category.deletedInd == False)

    if only_child:
        query = query.filter(Category.groupCategoryId != True)

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
            "bandSizeType":       r.bandSizeType,
            "hasCupSize":         r.hasCupSize,
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
        "bandSizeType":     cat.bandSizeType,
        "hasCupSize":       cat.hasCupSize,
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
        bandSizeType     = data.bandSizeType,
        hasCupSize       = data.hasCupSize if data.hasCupSize is not None else False,
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
    cat.categoryImage    = data.categoryImage
    cat.bandSizeType     = data.bandSizeType
    if data.hasCupSize is not None:
        cat.hasCupSize   = data.hasCupSize
    cat.modifiedDate     = datetime.now(timezone.utc)

    if data.groupCategoryId is not None:
        cat.groupCategoryId = data.groupCategoryId
    else:
        cat.groupCategoryId = data.parentCategoryId is None

    db.commit()
    return cat.categoryId


def delete_category(db: Session, category_id: int) -> bool:
    cat = db.query(Category).filter(
        Category.categoryId == category_id,
        Category.deletedInd == False
    ).first()
    if not cat:
        return False
    cat.deletedInd   = True
    cat.modifiedDate = datetime.now(timezone.utc)
    db.commit()
    return True


# ── Category sizes ────────────────────────────────────────────────────────────

def get_category_sizes(db: Session, category_id: int) -> Optional[dict]:
    """
    Returns the band sizes (filtered by bandSizeType) and cup sizes
    (if hasCupSize=True) that apply to every product in this category.
    bandSizeType: 'alpha' -> SizeType='alphabetical', 'numerical' -> SizeType='numerical'
    """
    cat = db.query(Category).filter(
        Category.categoryId == category_id,
        Category.deletedInd == False
    ).first()
    if not cat:
        return None

    band_size_type = cat.bandSizeType
    has_cup_size   = bool(cat.hasCupSize) if cat.hasCupSize is not None else False

    if band_size_type == "numerical":
        size_type_clause = "AND \"SizeType\" = 'numerical'"
    elif band_size_type == "alpha":
        size_type_clause = "AND \"SizeType\" = 'alphabetical'"
    else:
        size_type_clause = ""

    size_rows = db.execute(text(f"""
        SELECT "SizeId", "SizeLabel", "SizeCode", "SizeType", "OrderNo"
        FROM mdm."Size"
        WHERE ("DeletedInd" = false OR "DeletedInd" IS NULL)
          AND ("IsActive" = true OR "IsActive" IS NULL)
          {size_type_clause}
        ORDER BY "OrderNo" NULLS LAST, "SizeId"
    """)).fetchall()

    cup_rows = []
    if has_cup_size:
        cup_rows = db.execute(text("""
            SELECT "CupSizeId", "SizeLabel"
            FROM mdm."CupSize"
            WHERE ("DeletedInd" = false OR "DeletedInd" IS NULL)
              AND ("IsActive" = true OR "IsActive" IS NULL)
            ORDER BY "CupSizeId"
        """)).fetchall()

    return {
        "categoryId":   cat.categoryId,
        "bandSizeType": band_size_type,
        "hasCupSize":   has_cup_size,
        "bandSizes": [{
            "sizeId":    r[0],
            "sizeLabel": r[1],
            "sizeCode":  r[2],
            "sizeType":  r[3],
        } for r in size_rows],
        "cupSizes": [{
            "cupSizeId": r[0],
            "sizeLabel": r[1],
        } for r in cup_rows],
    }


# ── User-facing: nested category menu tree ────────────────────────────────────

def get_category_menu(db: Session) -> dict:
    categories = db.query(Category).filter(
        Category.deletedInd == False,
        Category.isActive   == True
    ).order_by(
        asc(Category.parentCategoryId),
        asc(Category.displayOrder)
    ).all()

    def _build(parent_id):
        result = []
        children = [c for c in categories if c.parentCategoryId == parent_id]
        for child in children:
            node = {
                "categoryId":       child.categoryId,
                "name":             child.name,
                "url":              child.url,
                "about":            child.about,
                "parentCategoryId": child.parentCategoryId,
                "groupCategoryId":  child.groupCategoryId,
                "children":         _build(child.categoryId),
            }
            result.append(node)
        return result

    return {"categories": _build(None)}


# ── Category product counts ───────────────────────────────────────────────────

def get_category_product_counts(db: Session) -> List[dict]:
    child_sql = text("""
        SELECT p."ChildCategoryId" AS category_id, COUNT(*) AS cnt
        FROM twam."ProductVariantDetail" pvd
        JOIN twam."Products" p ON p."ProductId" = pvd."ProductId"
        WHERE pvd."DeletedInd" = false AND p."DeletedInd" = false AND pvd."State" = 'Approved'
          AND p."ChildCategoryId" IS NOT NULL
        GROUP BY p."ChildCategoryId"
    """)
    parent_sql = text("""
        SELECT p."CategoryId" AS category_id, COUNT(*) AS cnt
        FROM twam."ProductVariantDetail" pvd
        JOIN twam."Products" p ON p."ProductId" = pvd."ProductId"
        WHERE pvd."DeletedInd" = false AND p."DeletedInd" = false AND pvd."State" = 'Approved'
          AND p."CategoryId" IS NOT NULL
        GROUP BY p."CategoryId"
    """)
    rows  = db.execute(child_sql).fetchall()
    rows += db.execute(parent_sql).fetchall()
    return [{"categoryId": r[0], "count": r[1]} for r in rows if r[0]]