import re
import json
from typing import Optional, List, Dict
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from sqlalchemy.orm import Session
from database import get_db
from app.auth.dependencies import get_current_user, require_roles, Roles
from app.shared.azure_storage import upload_image, delete_image
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
# PUBLIC endpoints — MUST be before /{category_id} catch-all
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
    return repository.get_all_categories(db, filters, o_asc, o_prop, p_index, p_size)


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN endpoints — role required
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


@router.put("/{category_id}",
            dependencies=[Depends(require_roles(Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER))])
def update_category(category_id: int, command: schemas.CategoryUpdate, db: Session = Depends(get_db)):
    result = repository.update_category(db, category_id, command)
    if not result: raise HTTPException(404, "Category not found.")
    return {"categoryId": result}


@router.delete("/{category_id}",
               dependencies=[Depends(require_roles(Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER))])
def delete_category(category_id: int, db: Session = Depends(get_db)):
    result = repository.delete_category(db, category_id)
    if not result:
        raise HTTPException(404, "Category not found.")
    return result
@router.get("/GetCategory/{category_id}", response_model=schemas.CategoryResponse)
def get_category_public(category_id: int, db: Session = Depends(get_db)):
    """Public endpoint — fetch a single category by ID (no auth required)."""
    result = repository.get_category_by_id(db, category_id)
    if not result:
        raise HTTPException(404, "Category not found.")
    return result


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

    # Resolve to absolute URL for local /uploads/ paths
    if new_url and new_url.startswith("/uploads/"):
        new_url = str(request.base_url).rstrip("/") + new_url

    cat.categoryImage = new_url
    from datetime import datetime, timezone
    cat.modifiedDate = datetime.now(timezone.utc)
    db.commit()

    if old_url and old_url != new_url:
        delete_image(old_url)

    return {"success": True, "categoryId": category_id, "imageUrl": new_url}




@router.patch(
    "/{category_id}/visibility",
    dependencies=[Depends(require_roles(Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER))],
)
def toggle_category_visibility(
    category_id: int,
    unhide_children: bool = Query(False, alias="unhideChildren"),
    db: Session = Depends(get_db),
):
    """Toggle isActive (hide / unhide) for a category.
    Hiding a parent cascades to all its child categories.
    When un-hiding, pass ?unhideChildren=true to also restore child categories.
    """
    result = repository.toggle_category_visibility(db, category_id, unhide_children)
    if not result:
        raise HTTPException(404, "Category not found.")
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

@router.get("/{category_id}", response_model=schemas.CategoryResponse,
            dependencies=[Depends(require_roles(Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER))])
def get_category(category_id: int, db: Session = Depends(get_db)):
    result = repository.get_category_by_id(db, category_id)
    if not result: raise HTTPException(404, "Category not found.")
    return result

@router.get(
    "/{category_id}/children",
    dependencies=[Depends(require_roles(Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER))],
)
def get_category_children(category_id: int, db: Session = Depends(get_db)):
    """Return direct child categories for a given parent category ID."""
    children = repository.get_children(db, category_id)
    return [{"categoryId": c.categoryId, "name": c.name, "isActive": c.isActive} for c in children]
