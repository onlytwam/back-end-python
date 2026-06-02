import re
import json
from typing import Optional, List, Dict
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
from database import get_db
from app.auth.dependencies import get_current_user, Roles
from . import schemas, repository
from app.productvariant import repository as variant_repo

router = APIRouter(prefix="/api/ProductVariantDetail", tags=["ProductVariantDetail"])


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

# ── Find by Stock ID — MUST be first to avoid /{pvd_id} catch-all ────────────
@router.get("/FindByStockId", dependencies=[Depends(get_current_user)])
def find_by_stock_id(
    stockId: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
):
    """
    Exact (case-insensitive) lookup of a ProductVariantDetail by StockId.
    Returns productId + productVariantId + sizeId + cupSizeId so the
    admin frontend can deep-link directly to the right variant tile.
    Returns 404 when no match is found.
    """
    result = repository.find_by_stock_id(db, stockId.strip())
    if not result:
        raise HTTPException(
            status_code=404,
            detail=f"No variant detail found with Stock ID '{stockId}'",
        )
    return result

# PUBLIC endpoints — MUST be defined BEFORE /{pvd_id} catch-all
# FastAPI matches routes top-to-bottom, so specific paths must come first
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/UserProductVariantDetail", response_model=schemas.UserProductListResponse)
def get_user_product_list(
    request: Request,
    page_index:      Optional[int]  = Query(None, alias="page.index"),
    page_size:       Optional[int]  = Query(None, alias="page.size"),
    order_property:  Optional[str]  = Query(None, alias="order.property"),
    order_ascending: Optional[bool] = Query(None, alias="order.ascending"),
    db: Session = Depends(get_db)
):
    filters    = parse_angular_filters(request)
    p_index    = page_index  if page_index  and page_index  > 0 else 1
    p_size     = page_size   if page_size   and page_size   > 0 else 10
    order_asc  = order_ascending if order_ascending is not None else False
    order_prop = order_property or "ModifiedDate"
    return variant_repo.get_user_product_list(
        db, filters, p_index, p_size, order_prop, order_asc
    )


@router.get("/UserProductVariant/{variant_id}")
def get_user_product_variant(variant_id: int, db: Session = Depends(get_db)):
    result = variant_repo.get_user_product_variant(db, product_variant_id=variant_id)
    if not result: raise HTTPException(404, "Product variant not found.")
    return result


@router.get("/Product", response_model=schemas.UserProductListResponse)
def get_product_by_name(name: str = Query(...), db: Session = Depends(get_db)):
    result = variant_repo.get_user_product_variant(db, name=name)
    if not result: raise HTTPException(404, "Product not found.")
    return result


@router.get("/NewArrivals", response_model=schemas.UserProductListResponse)
def get_new_arrivals(
    page_index: Optional[int] = Query(1,  alias="page.index"),
    page_size:  Optional[int] = Query(40, alias="page.size"),
    db: Session = Depends(get_db)
):
    """
    Return product variants added in the last 6 months, newest first.
    Public endpoint — no authentication required.
    """
    return variant_repo.get_new_arrivals(
        db,
        page_index=page_index or 1,
        page_size=page_size  or 40,
    )


@router.get("/ProductDetail/{pvd_id}", response_model=schemas.UserProductDetailResponse)
def get_user_product_detail(pvd_id: int, db: Session = Depends(get_db)):
    result = repository.get_user_product_detail(db, pvd_id)
    if not result: raise HTTPException(404, "Product variant detail not found.")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN endpoints — require login — defined AFTER all specific public routes
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/AdminSizePalette/{variant_id}", dependencies=[Depends(get_current_user)])
def get_admin_size_palette(
    variant_id: int,
    isAlphaSize: Optional[bool] = Query(None, alias="isAlphaSize"),
    isCupSize:   Optional[bool] = Query(None, alias="isCupSize"),
    db: Session = Depends(get_db),
):
    """
    Returns all MDM band sizes and cup sizes with embedded PVD data for the
    given variant. Used by the admin product detail size palette component.

    isAlphaSize / isCupSize query params let the frontend pass the live
    (unsaved) toggle state so sizes refresh instantly on checkbox change
    without requiring a save first.
    """
    return repository.get_admin_size_palette(
        db, variant_id,
        is_alpha_override=isAlphaSize,
        is_cup_override=isCupSize,
    )


@router.get("/", response_model=schemas.ProductVariantDetailListResponse,
            dependencies=[Depends(get_current_user)], include_in_schema=False)
@router.get("", response_model=schemas.ProductVariantDetailListResponse,
            dependencies=[Depends(get_current_user)])
def get_variant_details(
    Filters: Optional[str] = Query(None, alias="Filters"),
    Order_Ascending: Optional[bool] = Query(None, alias="Order.Ascending"),
    Order_Property: Optional[str] = Query(None, alias="Order.Property"),
    Page_Index: Optional[int] = Query(None, alias="Page.Index"),
    Page_Size: Optional[int] = Query(None, alias="Page.Size"),
    db: Session = Depends(get_db)
):
    return repository.get_all_variant_details(
        db, parse_filters(Filters), Order_Ascending, Order_Property, Page_Index, Page_Size
    )


@router.post("/", status_code=201, dependencies=[Depends(get_current_user)], include_in_schema=False)
@router.post("", status_code=201, dependencies=[Depends(get_current_user)])
def create_variant_detail(command: schemas.ProductVariantDetailCreate, db: Session = Depends(get_db)):
    try:
        new_id = repository.create_variant_detail(db, command)
    except Exception as exc:
        # Roll back any partial transaction and surface the real DB error message
        try:
            db.rollback()
        except Exception:
            pass
        err_msg = str(exc)
        # Show the full error so we can see which column is null
        import re as _re
        col_match = _re.search(r'null value in column "(.*?)"', err_msg)
        if col_match:
            err_msg = f'Column "{col_match.group(1)}" is NOT NULL in DB but ORM sent null — add it to the model'
        raise HTTPException(status_code=500, detail=f"Failed to add size: {err_msg}")
    # ── Audit log ─────────────────────────────────────────────────────────────
    try:
        from app.stocklog.repository import add_audit_event as add_event
        add_event(
            db,
            product_id               = getattr(command, "productId", 0),
            product_variant_id       = getattr(command, "productVariantId", None),
            product_variant_detail_id = new_id,
            event_type               = "SIZE_ADDED",
            event_source             = "ProductDetail",
            description              = f"Size added — stock: {getattr(command, 'stockQuantity', 0)}",
            stock_before             = None,
            stock_after              = getattr(command, "stockQuantity", None),
            changed_by               = getattr(command, "createdBy", None) or "Admin",
            product_code             = getattr(command, "productCode", None),
            stock_id                 = getattr(command, "stockId", None),
        )
    except Exception:
        pass
    return {"productVariantDetailId": new_id}


@router.post("/CheckValidations", response_model=schemas.CheckVariantDetailResponse,
             dependencies=[Depends(get_current_user)])
def check_validations(command: schemas.CheckVariantDetailRequest, db: Session = Depends(get_db)):
    count = repository.check_duplicate(
        db, command.productId, command.productVariantId,
        command.sizeId, command.cupSizeId, command.productVariantDetailId or 0
    )
    return {"validationCount": count}


@router.put("/{pvd_id}", dependencies=[Depends(get_current_user)])
def update_variant_detail(pvd_id: int, command: schemas.ProductVariantDetailUpdate, db: Session = Depends(get_db)):
    result = repository.update_variant_detail(db, pvd_id, command)
    if not result: raise HTTPException(404, "ProductVariantDetail not found.")
    return {"productVariantDetailId": result}



# ── toggle variant detail active/inactive (isActive only — NOT deletedInd) ───
@router.patch("/{pvd_id}/toggle-active", dependencies=[Depends(get_current_user)])
def toggle_variant_detail_active(pvd_id: int, db: Session = Depends(get_db)):
    from app.productvariantdetail.models import ProductVariantDetail
    from datetime import datetime, timezone
    pvd = db.query(ProductVariantDetail).filter(
        ProductVariantDetail.productVariantDetailId == pvd_id,
        ProductVariantDetail.deletedInd == False,
    ).first()
    if not pvd:
        raise HTTPException(status_code=404, detail="Variant detail not found.")
    new_state        = not bool(pvd.isActive)
    pvd.isActive     = new_state
    pvd.modifiedDate = datetime.now(timezone.utc)
    db.commit()
    return {"pvdId": pvd_id, "isActive": new_state}

@router.delete("/{pvd_id}", status_code=204, dependencies=[Depends(get_current_user)])
def delete_variant_detail(pvd_id: int, db: Session = Depends(get_db)):
    # Snapshot before deletion for audit log
    pvd_snap = None
    try:
        pvd_snap = repository.get_variant_detail_by_id(db, pvd_id)
    except Exception:
        pass
    if not repository.delete_variant_detail(db, pvd_id):
        raise HTTPException(404, "ProductVariantDetail not found.")
    # ── Audit log ─────────────────────────────────────────────────────────────
    try:
        from app.stocklog.repository import add_audit_event as add_event
        add_event(
            db,
            product_id               = pvd_snap.get("productId", 0) if pvd_snap else 0,
            product_variant_id       = pvd_snap.get("productVariantId") if pvd_snap else None,
            product_variant_detail_id = pvd_id,
            event_type               = "SIZE_DELETED",
            event_source             = "ProductDetail",
            description              = f"Size deleted [PVD {pvd_id}] — stock was: {pvd_snap.get('stockQuantity', 0) if pvd_snap else '?'}",
            stock_before             = pvd_snap.get("stockQuantity") if pvd_snap else None,
            stock_after              = None,
            changed_by               = "Admin",
            product_code             = pvd_snap.get("productCode") if pvd_snap else None,
            stock_id                 = pvd_snap.get("stockId") if pvd_snap else None,
        )
    except Exception:
        pass



# /{pvd_id} MUST be last — it's a catch-all for any integer path segment
@router.get("/{pvd_id}", response_model=schemas.ProductVariantDetailResponse,
            dependencies=[Depends(get_current_user)])
def get_variant_detail(pvd_id: int, db: Session = Depends(get_db)):
    result = repository.get_variant_detail_by_id(db, pvd_id)
    if not result: raise HTTPException(404, "ProductVariantDetail not found.")
    return result