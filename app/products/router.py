# app/products/router.py
import json
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
from database import get_db
from . import schemas, repository
from app.auth.dependencies import get_current_user, require_roles, Roles, CurrentUser

router = APIRouter(prefix="/api/Products", tags=["Products"])


def parse_filters(raw: Optional[str]) -> Optional[list]:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else [parsed]
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid Filters format.")


# ── PUBLIC: list products ─────────────────────────────────────────────────────
@router.get("/", response_model=schemas.ProductListResponse)
def get_products(
    Filters:         Optional[str]  = Query(None, alias="Filters"),
    Order_Ascending: Optional[bool] = Query(None, alias="Order.Ascending"),
    Order_Property:  Optional[str]  = Query(None, alias="Order.Property"),
    Page_Index:      Optional[int]  = Query(None, alias="Page.Index", ge=1),
    Page_Size:       Optional[int]  = Query(None, alias="Page.Size",  ge=1),
    adminView:       bool           = Query(False, alias="adminView"),
    db: Session = Depends(get_db),
):
    return repository.get_all_products(
        db=db,
        filters=parse_filters(Filters),
        order_ascending=Order_Ascending,
        order_property=Order_Property,
        page_index=Page_Index,
        page_size=Page_Size,
        public_only=not adminView,
    )


# ── ADMIN: search products by name or productCode ────────────────────────────
# Must be defined BEFORE /{product_id} to avoid catch-all interception
@router.get("/Search", response_model=schemas.ProductListResponse,
            dependencies=[Depends(get_current_user)])
def search_products(
    q:          str           = Query(..., min_length=1, description="Search term for name or product code"),
    Page_Index: Optional[int] = Query(1,   alias="Page.Index", ge=1),
    Page_Size:  Optional[int] = Query(20,  alias="Page.Size",  ge=1),
    db: Session = Depends(get_db),
):
    """
    Search products by name OR productCode (case-insensitive contains).
    Returns the same ProductListResponse shape as the main listing.
    """
    filters = [
        {"property": "name",        "comparison": "contains", "value": q},
        {"property": "productCode", "comparison": "contains", "value": q},
    ]
    return repository.search_products_by_name_or_code(
        db, q=q,
        page_index=Page_Index or 1,
        page_size=Page_Size  or 20,
    )


# ── ADMIN: find product by StockId (on any variant detail) ───────────────────
@router.get("/SearchByStockId", response_model=schemas.ProductListResponse,
            dependencies=[Depends(get_current_user)])
def search_by_stock_id(
    stockId: str = Query(..., min_length=1, description="StockId to look up in ProductVariantDetail"),
    db: Session  = Depends(get_db),
):
    """
    Find the product(s) that own a ProductVariantDetail with the given StockId.
    Exact match — returns the parent product card(s).
    """
    return repository.search_products_by_stock_id(db, stock_id=stockId.strip())


# ── PUBLIC: get single product ────────────────────────────────────────────────
@router.get("/{product_id}", response_model=schemas.ProductResponse)
def get_product(product_id: int, db: Session = Depends(get_db)):
    result = repository.get_product_by_id(db, product_id)
    if not result:
        raise HTTPException(status_code=404, detail="Product not found.")
    return result


# ── PROTECTED: create product ─────────────────────────────────────────────────
@router.post("/", status_code=201,
             dependencies=[Depends(require_roles(Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER))])
def create_product(command: schemas.ProductCreate, request: Request, db: Session = Depends(get_db)):
    new_id = repository.create_product(db, command)

    # ── Audit log ─────────────────────────────────────────────────────────────
    try:
        from app.stocklog.repository import add_audit_event as add_event
        source = request.headers.get("X-Event-Source", "ProductDetail")
        add_event(
            db,
            product_id   = new_id,
            event_type   = "PRODUCT_CREATED",
            event_source = source,
            description  = f"Product created: {getattr(command, 'name', '') or ''} [{getattr(command, 'productCode', '') or ''}]",
            changed_by   = getattr(command, "createdBy", None) or "system",
            product_code = getattr(command, "productCode", None),
            product_name = getattr(command, "name", None),
        )
    except Exception:
        pass

    return {"productId": new_id}


# ── PROTECTED: update product ─────────────────────────────────────────────────
@router.put("/{product_id}",
            dependencies=[Depends(require_roles(Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER,
                                                Roles.INVENTORY_MANAGER))])
def update_product(product_id: int, command: schemas.ProductUpdate, db: Session = Depends(get_db)):
    result = repository.update_product(db, product_id, command)
    if not result:
        raise HTTPException(status_code=404, detail="Product not found.")
    return {"productId": result}


# ── PROTECTED: toggle active/inactive ────────────────────────────────────────
@router.patch("/{product_id}/toggle-active",
              dependencies=[Depends(require_roles(Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER))])
def toggle_product_active(product_id: int, db: Session = Depends(get_db)):
    result = repository.toggle_product_active(db, product_id)
    if not result:
        raise HTTPException(status_code=404, detail="Product not found.")
    return result


# ── PROTECTED: delete product ─────────────────────────────────────────────────
@router.delete("/{product_id}", status_code=204,
               dependencies=[Depends(require_roles(Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER))])
def delete_product(product_id: int, db: Session = Depends(get_db)):
    # Fetch name before deletion for the audit log
    product = None
    try:
        product = repository.get_product_by_id(db, product_id)
    except Exception:
        pass

    result = repository.delete_product(db, product_id)
    if not result:
        raise HTTPException(status_code=404, detail="Product not found.")

    # ── Audit log ─────────────────────────────────────────────────────────────
    try:
        from app.stocklog.repository import add_audit_event as add_event
        add_event(
            db,
            product_id   = product_id,
            event_type   = "PRODUCT_DELETED",
            event_source = "ProductDetail",
            description  = f"Product deleted: {product.get('name', '') if product else ''} [{product.get('productCode', '') if product else product_id}]",
            changed_by   = "Admin",
            product_code = product.get("productCode") if product else None,
            product_name = product.get("name") if product else None,
        )
    except Exception:
        pass


# ── PROTECTED: validate product ───────────────────────────────────────────────
@router.post("/CheckProductValidations", response_model=schemas.ProductValidationResponse,
             dependencies=[Depends(get_current_user)])
def check_product_validations(command: schemas.ProductValidationRequest, db: Session = Depends(get_db)):
    is_valid = repository.check_product_validations(db, command.productId, command.state)
    return {"isValid": is_valid}

# ── ADMIN: retroactively recalculate CGST/SGST for one product's PVDs ────────
@router.post(
    "/{product_id}/recalculate-taxes",
    dependencies=[Depends(require_roles(Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER))],
    summary="Recalculate CGST/SGST on all PVDs for a product",
)
def recalculate_product_taxes(product_id: int, db: Session = Depends(get_db)):
    """
    Re-computes CGST, SGST, and TaxAmount for every active PVD of this product
    using the product's current TaxHSNCode and each PVD's FinalPrice.

    Use this when:
    - A TaxHSNCode was attached to an existing product that already had PVDs.
    - The product's HSN code was changed and you want to force a re-sync.
    - You need to confirm existing PVD tax values are up to date.
    """
    result = repository.recalculate_pvd_taxes_for_product(db, product_id)
    return {
        "productId": product_id,
        **result,
    }


# ── ADMIN: retroactively recalculate ALL products that have a TaxHSNCode ─────
@router.post(
    "/bulk-recalculate-taxes",
    dependencies=[Depends(require_roles(Roles.SUPER_ADMIN))],
    summary="Recalculate CGST/SGST across all products that have an HSN code",
)
def bulk_recalculate_all_taxes(db: Session = Depends(get_db)):
    """
    Sweeps every product that has a TaxHSNCodeId set and re-computes CGST/SGST
    on all its PVDs.  Useful for one-time data fixes or after a GST rate change.

    This operation can be slow on large catalogs — it processes every PVD row.
    Errors on individual PVDs are collected but do not abort the sweep.
    """
    rows = db.execute(
        __import__("sqlalchemy").text(
            'SELECT DISTINCT "ProductId" FROM twam."Products" '            'WHERE "TaxHSNCodeId" IS NOT NULL AND "DeletedInd" = false'
        )
    ).fetchall()

    product_ids = [r[0] for r in rows]
    total_updated = total_skipped = 0
    all_errors: list = []

    for pid in product_ids:
        result = repository.recalculate_pvd_taxes_for_product(db, pid)
        total_updated += result.get("updated", 0)
        total_skipped += result.get("skipped", 0)
        all_errors.extend(result.get("errors", []))

    return {
        "productsProcessed": len(product_ids),
        "pvdsUpdated":       total_updated,
        "pvdsSkipped":       total_skipped,
        "errors":            all_errors,
    }