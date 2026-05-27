from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from database import get_db
from app.auth.dependencies import get_current_user
from . import repository

router = APIRouter(prefix="/api/ProductAuditLog", tags=["ProductAuditLog"])


@router.get("/product/{product_id}", dependencies=[Depends(get_current_user)])
def get_product_history(
    product_id: int,
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """Full chronological history for one product."""
    return repository.get_product_history(db, product_id, limit=limit)


@router.get("/all", dependencies=[Depends(get_current_user)])
def get_all_history(
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Paginated global history across all products (newest first)."""
    return repository.get_all_history(db, page=page, size=size)
