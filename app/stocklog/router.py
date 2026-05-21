# Stock Log module — add to main.py:
#   from app.stocklog.router import router as stocklog_router
#   app.include_router(stocklog_router)
#
# No SQL migration needed — Base.metadata.create_all creates twam."StockLog"
# automatically on first startup once this module is imported.
# The new StockId column IS added via Alembic / ALTER TABLE — see migration note below.

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
    return repository.get_pvds_for_variant(db, variant_id)


@router.get("/logs/{pvd_id}", dependencies=[Depends(get_current_user)])
def get_logs(
    pvd_id: int,
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
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
    result = repository.update_stock_with_log(
        db,
        pvd_id=pvd_id,
        stock_quantity=body.stockQuantity,
        processed_quantity=body.processedQuantity,
        returned_quantity=body.returnedQuantity,
        note=body.note,
        changed_by=body.changedBy,
    )
    if result is None:
        raise HTTPException(404, "ProductVariantDetail not found.")
    return result


@router.post("/backfill", dependencies=[Depends(get_current_user)])
def backfill_stock_logs(db: Session = Depends(get_db)):
    """
    One-time backfill: seeds StockLog with INITIAL_STOCK entries for every
    existing ProductVariantDetail that has no log yet.
    Safe to call multiple times — already-logged PVDs are skipped.
    """
    result = repository.backfill_initial_stock(db)
    return {
        "message": f"Backfill complete. Seeded {result['seeded']} entries, skipped {result['skipped']} already-logged.",
        **result,
    }