import re
import json
from datetime import datetime, timezone
from typing import Optional, List, Dict
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from sqlalchemy.orm import Session
from database import get_db
from app.auth.dependencies import get_current_user, require_roles, Roles
from app.shared.azure_storage import upload_image, delete_image
from app.shared.url_helpers import resolve_url
from . import schemas, repository

router = APIRouter(prefix="/api/Category", tags=["Category"])


def parse_filters(raw):
    if not raw: return None
    try:
        p = json.loads(raw)
        return p if isinstance(p, list) else [p]
    except: raise HTTPException(400, "Invalid Filters format.")


def parse_angular_filters(request: Request) -> List[Dict]:
    raw = dict(request.query_params)
    bucket: Dict[int, Dict] = {}
    for key, value in raw.items():
        m = re.match(r'filters\[(\d+)\]\.(\w+)$', key, re.IGNORECASE)
        if m:
            idx = int(m.group(1))
            field = m.group(2).lower()
            if idx not in bucket:
                bucket[idx] = {}
            bucket[idx][field] = value
    result = []
    for i in sorted(bucket.keys()):
        f = bucket[i]
        if f.get("property") and f.get("value") not in (None, ""):
            result.append({
                "property":   f["property"],
                "comparison": f.get("comparison", "contains"),
                "value":      f["value"],
            })
    return result


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC endpoints — no auth required
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/GetCategory", response_model=schemas.CategoryMenuResponse)
def get_category_menu(command: schemas.GetCategoryRequest, db: Session = Depends(get_db)):
    return repository.get_category_menu(db)


@router.get("/GetCategoryProductCount", response_model=list)
def get_category_product_counts(db: Session = Depends(get_db)):
    return repository.get_category_product_counts(db)


@router.get("/GetProductDetailCategory", response_model=schemas.CategoryListResponse)
def get_product_detail_categories(
    request: Request,
    page_index:      Optional[int]  = Query(None, alias="page.index"),
    page_size:       Optional[int]  = Query(None, alias="page.size"),
    order_property:  Optional[str]  = Query(None, alias="order.property"),
    order_ascending: Optional[bool] = Query(None, alias="order.ascending"),
    Page_Index:      Optional[int]  = Query(None, alias="Page.Index"),
    Page_Size:       Optional[int]  = Query(None, alias="Page.Size"),
    Order_Property:  Optional[str]  = Query(None, alias="Order.Property"),
    Order_Ascending: Optional[bool] = Query(None, alias="Order.Ascending"),
    Filters:         Optional[str]  = Query(None, alias="Filters"),
    db: Session = Depends(get_db)
):
    p_index = (page_index or Page_Index) if ((page_index or Page_Index) or 0) > 0 else None
    p_size  = (page_size  or Page_Size)  if ((page_size  or Page_Size)  or 0) > 0 else None
    o_prop  = order_property or Order_Property
    o_asc   = order_ascending if order_ascending is not None else Order_Ascending

    filters = parse_angular_filters(request) or parse_filters(Filters) or []
    return repository.get_all_categories(
        db, filters, o_asc, o_prop, p_index, p_size,
        with_products_only=True,
    )


# FIX: /GetCategory/{category_id} MUST be registered before /{category_id}
# otherwise FastAPI's wildcard /{category_id} catches it first and the
# require_roles auth guard rejects the request with 403.
@router.get("/GetCategory/{category_id}", response_model=schemas.CategoryResponse)
def get_category_public(category_id: int, db: Session = Depends(get_db)):
    """Public endpoint — fetch a single category by ID (no auth required)."""
    result = repository.get_category_by_id(db, category_id)
    if not result:
        raise HTTPException(404, "Category not found.")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN endpoints — role required
# All specific sub-paths (check-delete, children, image, visibility) MUST be
# declared before the plain /{category_id} wildcard route so FastAPI matches
# them correctly instead of hitting the wildcard first.
# ─────────────────────────────────────────────────────────────────────────────

@router.get("", response_model=schemas.CategoryListResponse,
            dependencies=[Depends(require_roles(Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER, Roles.INVENTORY_MANAGER))])
def get_categories(
    request: Request,
    page_index:      Optional[int]  = Query(None, alias="page.index"),
    page_size:       Optional[int]  = Query(None, alias="page.size"),
    order_property:  Optional[str]  = Query(None, alias="order.property"),
    order_ascending: Optional[bool] = Query(None, alias="order.ascending"),
    Page_Index:      Optional[int]  = Query(None, alias="Page.Index"),
    Page_Size:       Optional[int]  = Query(None, alias="Page.Size"),
    Order_Property:  Optional[str]  = Query(None, alias="Order.Property"),
    Order_Ascending: Optional[bool] = Query(None, alias="Order.Ascending"),
    Filters:         Optional[str]  = Query(None, alias="Filters"),
    db: Session = Depends(get_db)
):
    p_index = (page_index or Page_Index) if ((page_index or Page_Index) or 0) > 0 else None
    p_size  = (page_size  or Page_Size)  if ((page_size  or Page_Size)  or 0) > 0 else None
    o_prop  = order_property or Order_Property
    o_asc   = order_ascending if order_ascending is not None else Order_Ascending

    raw_filters = parse_angular_filters(request) or parse_filters(Filters) or []
    only_child = any(
        f.get("property") == "IsSelectCategory" and
        str(f.get("value", "false")).lower() in ("true", "1")
        for f in raw_filters
    )
    return repository.get_all_categories(db, raw_filters, o_asc, o_prop, p_index, p_size, only_child)


@router.post("", status_code=201,
             dependencies=[Depends(require_roles(Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER))])
def create_category(command: schemas.CategoryCreate, db: Session = Depends(get_db)):
    return {"categoryId": repository.create_category(db, command)}


# FIX: /{category_id}/check-delete declared before /{category_id} wildcard GET
@router.get(
    "/{category_id}/check-delete",
    dependencies=[Depends(require_roles(Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER))],
)
def check_category_delete(category_id: int, db: Session = Depends(get_db)):
    """
    Pre-flight check before deletion.
    Returns whether the category is a parent, how many children it has,
    and how many products (across itself and all children) will be hidden.
    The frontend uses this to show a confirmation popup before calling DELETE.
    """
    cat = db.query(repository.Category).filter(
        repository.Category.categoryId == category_id,
        repository.Category.deletedInd == False,
    ).first()
    if not cat:
        raise HTTPException(404, "Category not found.")

    children = repository.get_children(db, category_id)
    child_ids = [c.categoryId for c in children]
    all_ids   = [category_id] + child_ids

    from sqlalchemy import text
    product_count_row = db.execute(text("""
        SELECT COUNT(DISTINCT p."ProductId")
        FROM twam."Products" p
        JOIN twam."ProductVariantDetail" pvd ON pvd."ProductId" = p."ProductId"
        WHERE p."DeletedInd"   = false
          AND pvd."DeletedInd" = false
          AND (p."CategoryId" = ANY(:ids) OR p."ChildCategoryId" = ANY(:ids))
    """), {"ids": all_ids}).fetchone()

    return {
        "categoryId":   category_id,
        "name":         cat.name,
        "isParent":     len(child_ids) > 0,
        "childCount":   len(child_ids),
        "productCount": int(product_count_row[0]) if product_count_row else 0,
        "children":     [{"categoryId": c.categoryId, "name": c.name} for c in children],
    }


# FIX: /{category_id}/children declared before /{category_id} wildcard GET
@router.get(
    "/{category_id}/children",
    dependencies=[Depends(require_roles(Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER))],
)
def get_category_children(category_id: int, db: Session = Depends(get_db)):
    """Return direct child categories for a given parent category ID."""
    children = repository.get_children(db, category_id)
    return [{"categoryId": c.categoryId, "name": c.name, "isActive": c.isActive} for c in children]


@router.put(
    "/{category_id}/image",
    dependencies=[Depends(require_roles(Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER))],
)
def upload_category_image(
    category_id: int,
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload / replace the image for a category (admin only)."""
    cat = db.query(repository.Category).filter(
        repository.Category.categoryId == category_id,
        repository.Category.deletedInd == False,
    ).first()
    if not cat:
        raise HTTPException(404, "Category not found.")

    old_url = cat.categoryImage or ""
    new_url = upload_image(file, container_key="category", folder=f"category_{category_id}")

    # Store raw path — resolve_url() handles display at read time
    cat.categoryImage = new_url
    cat.modifiedDate  = datetime.now(timezone.utc)
    db.commit()

    if old_url and old_url != new_url:
        delete_image(old_url)

    return {"success": True, "categoryId": category_id, "imageUrl": resolve_url(new_url)}




@router.patch(
    "/reorder",
    dependencies=[Depends(require_roles(Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER))],
)
def reorder_categories(
    body: List[Dict],
    db: Session = Depends(get_db),
):
    """Bulk-update displayOrder for a list of categories.

    Expects a JSON array of objects with 'categoryId' and 'displayOrder':
        [{"categoryId": 1, "displayOrder": 1}, {"categoryId": 2, "displayOrder": 2}, ...]
    """
    if not body:
        raise HTTPException(400, "No items provided.")

    ids = [item.get("categoryId") for item in body if item.get("categoryId")]
    cats = db.query(repository.Category).filter(
        repository.Category.categoryId.in_(ids),
        repository.Category.deletedInd == False,
    ).all()

    cat_map = {c.categoryId: c for c in cats}
    now = datetime.now(timezone.utc)
    updated = 0
    for item in body:
        cid   = item.get("categoryId")
        order = item.get("displayOrder")
        if cid is None or order is None:
            continue
        cat = cat_map.get(cid)
        if cat:
            cat.displayOrder = int(order)
            cat.modifiedDate = now
            updated += 1

    db.commit()
    return {"updated": updated}


@router.patch(
    "/{category_id}/visibility",
    dependencies=[Depends(require_roles(Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER))],
)
def toggle_category_visibility(
    category_id: int,
    unhide_children: bool = Query(False, alias="unhideChildren"),
    cascade_products: bool = Query(False, alias="cascadeProducts"),
    db: Session = Depends(get_db),
):
    """Toggle isActive (hide / unhide) for a category.
    Hiding a parent cascades to all its child categories.
    When un-hiding, pass ?unhideChildren=true to also restore child categories.
    Pass ?cascadeProducts=true to also hide/show all products in this category.
    """
    result = repository.toggle_category_visibility(db, category_id, unhide_children)
    if not result:
        raise HTTPException(404, "Category not found.")
    if cascade_products:
        try:
            if result.get("isActive") is False:
                from app.products.repository import deactivate_products_by_category
                deactivate_products_by_category(db, category_id)
            else:
                from app.products.repository import reactivate_products_by_category
                reactivate_products_by_category(db, category_id)
        except Exception:
            pass
    return result


@router.delete(
    "/{category_id}/image",
    dependencies=[Depends(require_roles(Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER))],
)
def delete_category_image(category_id: int, db: Session = Depends(get_db)):
    """Remove the hero/banner image for a category."""
    old_url = repository.delete_category_image(db, category_id)
    if old_url is None and not repository.get_category_by_id(db, category_id):
        raise HTTPException(404, "Category not found.")
    if old_url:
        delete_image(old_url)
    return {"success": True, "categoryId": category_id}


@router.put(
    "/{category_id}/mobile-banner",
    dependencies=[Depends(require_roles(Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER))],
)
def upload_mobile_banner_image(
    category_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload / replace the mobile banner image for a category (admin only).
    This image is served to the mobile app via GET /api/Category/{id}/mobile-banner
    and is NOT used on the desktop website.
    """
    cat = db.query(repository.Category).filter(
        repository.Category.categoryId == category_id,
        repository.Category.deletedInd == False,
    ).first()
    if not cat:
        raise HTTPException(404, "Category not found.")

    old_url = cat.mobileBannerImage or ""
    new_url = upload_image(file, container_key="category", folder=f"category_{category_id}_mobile")

    cat.mobileBannerImage = new_url
    cat.modifiedDate      = datetime.now(timezone.utc)
    db.commit()

    if old_url and old_url != new_url:
        delete_image(old_url)

    return {
        "success":    True,
        "categoryId": category_id,
        "imageUrl":   resolve_url(new_url),
    }


@router.delete(
    "/{category_id}/mobile-banner",
    dependencies=[Depends(require_roles(Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER))],
)
def delete_mobile_banner_image(category_id: int, db: Session = Depends(get_db)):
    """Remove the mobile banner image for a category."""
    old_url = repository.delete_mobile_banner_image(db, category_id)
    if old_url is None and not repository.get_category_by_id(db, category_id):
        raise HTTPException(404, "Category not found.")
    if old_url:
        delete_image(old_url)
    return {"success": True, "categoryId": category_id}


@router.get(
    "/{category_id}/mobile-banner",
)
def get_mobile_banner_image(category_id: int, db: Session = Depends(get_db)):
    """Public endpoint — returns the mobile banner image URL for a category.
    Mobile team: call GET /api/Category/{categoryId}/mobile-banner to get the image.
    Response: { "categoryId": 1, "imageUrl": "https://..." }
    """
    cat = db.query(repository.Category).filter(
        repository.Category.categoryId == category_id,
        repository.Category.deletedInd == False,
    ).first()
    if not cat:
        raise HTTPException(404, "Category not found.")
    return {
        "categoryId": category_id,
        "imageUrl":   resolve_url(cat.mobileBannerImage) if cat.mobileBannerImage else None,
    }


@router.delete(
    "/{category_id}",
    dependencies=[Depends(require_roles(Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER))],
)
def delete_category(category_id: int, db: Session = Depends(get_db)):
    result = repository.delete_category(db, category_id)
    if not result:
        raise HTTPException(404, "Category not found.")
    # Cascade: set all products of this category to isActive=False
    try:
        from app.products.repository import deactivate_products_by_category
        deactivate_products_by_category(db, category_id)
    except Exception:
        pass
    return result


@router.put(
    "/{category_id}",
    dependencies=[Depends(require_roles(Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER))],
)
def update_category(category_id: int, command: schemas.CategoryUpdate, db: Session = Depends(get_db)):
    result = repository.update_category(db, category_id, command)
    if not result: raise HTTPException(404, "Category not found.")
    return {"categoryId": result}


# FIX: /{category_id} wildcard GET is now LAST — all specific sub-paths above
# take priority. Previously /GetCategory/{id} and /{id}/children were below
# this and got intercepted by the auth guard here, returning 403.
@router.get(
    "/{category_id}",
    response_model=schemas.CategoryResponse,
    dependencies=[Depends(require_roles(Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER))],
)
def get_category(category_id: int, db: Session = Depends(get_db)):
    result = repository.get_category_by_id(db, category_id)
    if not result: raise HTTPException(404, "Category not found.")
    return result