from datetime import datetime, timezone
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import text, desc

from .models import ProductAuditLog


def add_event(
    db: Session,
    *,
    product_id: int,
    event_type: str,
    event_source: Optional[str] = None,
    description: Optional[str] = None,
    product_variant_id: Optional[int] = None,
    product_variant_detail_id: Optional[int] = None,
    stock_before: Optional[int] = None,
    stock_after: Optional[int] = None,
    changed_by: Optional[str] = None,
    product_code: Optional[str] = None,
    product_name: Optional[str] = None,
    variant_name: Optional[str] = None,
    size_label: Optional[str] = None,
    stock_id: Optional[str] = None,
) -> None:
    """
    Insert one audit event. Always call inside a try/except so it never
    blocks the main operation if the audit write fails.
    """
    entry = ProductAuditLog(
        productId              = product_id,
        productVariantId       = product_variant_id,
        productVariantDetailId = product_variant_detail_id,
        eventType              = event_type,
        eventSource            = event_source,
        description            = description,
        stockBefore            = stock_before,
        stockAfter             = stock_after,
        changedBy              = changed_by,
        productCode            = product_code,
        productName            = product_name,
        variantName            = variant_name,
        sizeLabel              = size_label,
        stockId                = stock_id,
    )
    db.add(entry)
    db.commit()


def get_product_history(db: Session, product_id: int, limit: int = 200) -> list:
    """Full history for one product — all events sorted newest first."""
    rows = (
        db.query(ProductAuditLog)
        .filter(ProductAuditLog.productId == product_id)
        .order_by(desc(ProductAuditLog.createdDate))
        .limit(limit)
        .all()
    )
    return [_serialize(r) for r in rows]


def get_all_history(db: Session, page: int = 1, size: int = 50) -> dict:
    """Paginated list of all events across all products, newest first."""
    offset = (page - 1) * size
    total = db.query(ProductAuditLog).count()
    rows = (
        db.query(ProductAuditLog)
        .order_by(desc(ProductAuditLog.createdDate))
        .offset(offset)
        .limit(size)
        .all()
    )
    return {
        "total": total,
        "page":  page,
        "size":  size,
        "items": [_serialize(r) for r in rows],
    }


def _serialize(r: ProductAuditLog) -> dict:
    return {
        "productAuditLogId":      r.productAuditLogId,
        "productId":              r.productId,
        "productVariantId":       r.productVariantId,
        "productVariantDetailId": r.productVariantDetailId,
        "eventType":              r.eventType,
        "eventSource":            r.eventSource,
        "description":            r.description,
        "stockBefore":            r.stockBefore,
        "stockAfter":             r.stockAfter,
        "changedBy":              r.changedBy,
        "productCode":            r.productCode,
        "productName":            r.productName,
        "variantName":            r.variantName,
        "sizeLabel":              r.sizeLabel,
        "stockId":                r.stockId,
        "createdDate":            r.createdDate.isoformat() if r.createdDate else None,
    }
