# Stock Log module — add to main.py:
#   from app.stocklog.router import router as stocklog_router
#   app.include_router(stocklog_router)

from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from database import get_db
from app.auth.dependencies import get_current_user
from . import schemas, repository

router = APIRouter(prefix="/api/StockLog", tags=["StockLog"])


@router.get("/products", dependencies=[Depends(get_current_user)])
def list_products(
    page: int  = Query(1,  ge=1),
    size: int  = Query(50, ge=1, le=200),
    search: str = Query(""),
    db: Session = Depends(get_db),
):
    return repository.get_stock_summary(db, page=page, size=size, search=search)


@router.get("/variants/{product_id}", dependencies=[Depends(get_current_user)])
def list_variants(product_id: int, db: Session = Depends(get_db)):
    return repository.get_variants_for_product(db, product_id)


@router.get("/pvds/{variant_id}", dependencies=[Depends(get_current_user)])
def list_pvds(variant_id: int, db: Session = Depends(get_db)):
    """
    Returns PVDs for a variant.
    Automatically runs DB-change detection on each PVD.
    Each item has a `dbChangeAlert: bool` field — when true the admin
    should be notified that a direct database change was detected.
    """
    return repository.get_pvds_for_variant(db, variant_id)


@router.get("/logs/{pvd_id}", dependencies=[Depends(get_current_user)])
def get_logs(
    pvd_id: int,
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """
    Returns the stock change history for a PVD.
    INITIAL_STOCK entries are included in the raw log but excluded from
    the history UI on the frontend (they are system bootstrap entries).
    """
    logs = repository.get_logs_for_pvd(db, pvd_id, limit=limit)
    return [
        {
            "stockLogId":             l.stockLogId,
            "productVariantDetailId": l.productVariantDetailId,
            "stockId":                l.stockId,
            "changeType":             l.changeType,
            "previousValue":          l.previousValue,
            "newValue":               l.newValue,
            "totalStock":             l.totalStock,
            "processedQuantity":      l.processedQuantity,
            "returnedQuantity":       l.returnedQuantity,
            "availableQuantity":      l.availableQuantity,
            "note":                   l.note,
            "changedBy":              l.changedBy,
            "createdDate":            l.createdDate.isoformat() if l.createdDate else None,
        }
        for l in logs
    ]


@router.put("/update/{pvd_id}", dependencies=[Depends(get_current_user)])
def update_stock(
    pvd_id: int,
    body: schemas.StockUpdateRequest,
    db: Session = Depends(get_db),
):
    """
    The ONLY authoritative endpoint for manual stock changes.
    All changes are logged with changedBy='Admin' (or the value in body.changedBy).
    """
    result = repository.update_stock_with_log(
        db,
        pvd_id=pvd_id,
        stock_quantity=body.stockQuantity,
        processed_quantity=body.processedQuantity,
        returned_quantity=body.returnedQuantity,
        note=body.note,
        changed_by=body.changedBy or "Admin",
    )
    if result is None:
        raise HTTPException(404, "ProductVariantDetail not found.")
    return result



# ─── Product Audit History endpoints (folded into /api/StockLog) ──────────────

@router.get("/product-history/{product_id}", dependencies=[Depends(get_current_user)])
def get_product_audit_history(
    product_id: int,
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """
    Full chronological event history for one product.
    Covers: product created/deleted, variants added/deleted,
    sizes added/deleted, stock changes. Newest first.
    Frontend: GET /api/StockLog/product-history/{productId}
    """
    return repository.get_product_audit_history(db, product_id, limit=limit)


@router.get("/all-history", dependencies=[Depends(get_current_user)])
def get_all_audit_history(
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """
    Paginated global product lifecycle history across all products.
    Used by the 📋 History button in the Stock Management page header.
    Frontend: GET /api/StockLog/all-history
    """
    return repository.get_all_audit_history(db, page=page, size=size)


@router.get("/product-directory", dependencies=[Depends(get_current_user)])
def get_product_directory(
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    search: str = Query(""),
    db: Session = Depends(get_db),
):
    """
    Product-level history directory for the global History modal.
    Returns each product with name, code, image, creation info (date + who + role),
    and a list of structural modifications (variant/size added/deleted/updated).
    Frontend: GET /api/StockLog/product-directory
    """
    return repository.get_product_directory(db, page=page, size=size, search=search)

# ─── Excel Download endpoints ─────────────────────────────────────────────────

from fastapi.responses import Response

@router.get("/export/changes", dependencies=[Depends(get_current_user)])
def export_changes(db: Session = Depends(get_db)):
    """
    Download all product structural changes (variant/size added/deleted)
    as a formatted Excel file.
    """
    data = repository.export_changes_excel(db)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=product_changes.xlsx"},
    )


@router.get("/export/stock", dependencies=[Depends(get_current_user)])
def export_stock(db: Session = Depends(get_db)):
    """
    Download all Total Stock changes across all products as a formatted Excel file.
    """
    data = repository.export_stock_excel(db)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=stock_changes.xlsx"},
    )