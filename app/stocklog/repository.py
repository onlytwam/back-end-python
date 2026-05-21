from datetime import datetime, timezone
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import text

from .models import StockLog
from app.productvariantdetail.models import ProductVariantDetail


def _apply_stock_formula(pvd: ProductVariantDetail) -> None:
    total     = pvd.stockQuantity     or 0
    processed = pvd.processedQuantity or 0
    refunded  = pvd.returnedQuantity  or 0
    available = max(0, total - processed + refunded)
    pvd.availableQuantity = available
    pvd.isOutOfStock = available <= 0
    pvd.isLowStock   = 0 < available <= 3


def create_stock_log(
    db: Session,
    pvd_id: int,
    change_type: str,
    prev_val: Optional[int],
    new_val: Optional[int],
    pvd_snapshot: dict,
    note: Optional[str] = None,
    changed_by: Optional[str] = None,
) -> None:
    log = StockLog(
        productVariantDetailId = pvd_id,
        productId              = pvd_snapshot.get("productId"),
        productVariantId       = pvd_snapshot.get("productVariantId"),
        stockId                = pvd_snapshot.get("stockId"),
        changeType             = change_type,
        previousValue          = prev_val,
        newValue               = new_val,
        totalStock             = pvd_snapshot.get("stockQuantity"),
        processedQuantity      = pvd_snapshot.get("processedQuantity"),
        returnedQuantity       = pvd_snapshot.get("returnedQuantity"),
        availableQuantity      = pvd_snapshot.get("availableQuantity"),
        note                   = note,
        changedBy              = changed_by,
    )
    db.add(log)
    db.commit()


def get_logs_for_pvd(db: Session, pvd_id: int, limit: int = 100) -> List[StockLog]:
    return (
        db.query(StockLog)
        .filter(StockLog.productVariantDetailId == pvd_id)
        .order_by(StockLog.createdDate.desc())
        .limit(limit)
        .all()
    )


def update_stock_with_log(
    db: Session,
    pvd_id: int,
    stock_quantity: Optional[int],
    processed_quantity: Optional[int],
    returned_quantity: Optional[int],
    note: Optional[str],
    changed_by: Optional[str],
) -> Optional[dict]:
    pvd = db.query(ProductVariantDetail).filter(
        ProductVariantDetail.productVariantDetailId == pvd_id
    ).first()
    if not pvd:
        return None

    changes = []

    if stock_quantity is not None and stock_quantity != (pvd.stockQuantity or 0):
        changes.append(("TOTAL_STOCK", pvd.stockQuantity, stock_quantity))
        pvd.stockQuantity = stock_quantity

    if processed_quantity is not None and processed_quantity != (pvd.processedQuantity or 0):
        changes.append(("PROCESSED", pvd.processedQuantity, processed_quantity))
        pvd.processedQuantity = processed_quantity

    if returned_quantity is not None and returned_quantity != (pvd.returnedQuantity or 0):
        changes.append(("RETURNED", pvd.returnedQuantity, returned_quantity))
        pvd.returnedQuantity = returned_quantity

    _apply_stock_formula(pvd)
    pvd.modifiedDate = datetime.now(timezone.utc)
    db.commit()
    db.refresh(pvd)

    for change_type, prev_val, new_val in changes:
        log_entry = StockLog(
            productVariantDetailId = pvd_id,
            productId              = pvd.productId,
            productVariantId       = pvd.productVariantId,
            stockId                = pvd.stockId,
            changeType             = change_type,
            previousValue          = prev_val,
            newValue               = new_val,
            totalStock             = pvd.stockQuantity,
            processedQuantity      = pvd.processedQuantity,
            returnedQuantity       = pvd.returnedQuantity,
            availableQuantity      = pvd.availableQuantity,
            note                   = note,
            changedBy              = changed_by,
        )
        db.add(log_entry)
    db.commit()

    return {
        "productId":         pvd.productId,
        "productVariantId":  pvd.productVariantId,
        "stockId":           pvd.stockId,
        "stockQuantity":     pvd.stockQuantity,
        "processedQuantity": pvd.processedQuantity,
        "returnedQuantity":  pvd.returnedQuantity,
        "availableQuantity": pvd.availableQuantity,
    }


# ─── Backfill: seed StockLog from existing ProductVariantDetail rows ──────────

def backfill_initial_stock(db: Session) -> dict:
    """
    For every active ProductVariantDetail that has NO entry in StockLog yet,
    insert a single INITIAL_STOCK log row using the current stock values.
    Skips PVDs that already have at least one log entry.
    Returns counts: { seeded, skipped, total }.
    """
    # Find PVDs that have NO log entries at all
    pvds: List[ProductVariantDetail] = db.query(ProductVariantDetail).filter(
        ProductVariantDetail.deletedInd == False,
    ).all()

    # Collect PVD IDs that already have at least one log
    existing_pvd_ids: set = set()
    logged_ids = db.execute(
        text('SELECT DISTINCT "ProductVariantDetailId" FROM twam."StockLog"')
    ).fetchall()
    for row in logged_ids:
        existing_pvd_ids.add(row[0])

    seeded = 0
    skipped = 0

    for pvd in pvds:
        if pvd.productVariantDetailId in existing_pvd_ids:
            skipped += 1
            continue

        total     = pvd.stockQuantity     or 0
        processed = pvd.processedQuantity or 0
        returned  = pvd.returnedQuantity  or 0
        available = pvd.availableQuantity if pvd.availableQuantity is not None else max(0, total - processed + returned)

        log_entry = StockLog(
            productVariantDetailId = pvd.productVariantDetailId,
            productId              = pvd.productId,
            productVariantId       = pvd.productVariantId,
            stockId                = pvd.stockId,
            changeType             = "INITIAL_STOCK",
            previousValue          = None,
            newValue               = total,
            totalStock             = total,
            processedQuantity      = processed,
            returnedQuantity       = returned,
            availableQuantity      = available,
            note                   = "Auto-seeded from existing ProductVariantDetail data",
            changedBy              = "system",
        )
        db.add(log_entry)
        seeded += 1

    db.commit()
    return {"seeded": seeded, "skipped": skipped, "total": len(pvds)}


# ─── Stock summary (used by admin stock page) ─────────────────────────────────

def get_stock_summary(db: Session, page: int = 1, size: int = 50, search: str = "") -> dict:
    """
    Fetch paginated product list with stock info.
    Returns products joined with first image, grouped at product level.
    """
    offset = (page - 1) * size
    search_clause = ""
    params: dict = {"limit": size, "offset": offset}

    if search:
        search_clause = """
            AND (
                LOWER(p."Name") LIKE :search
                OR LOWER(p."ProductCode") LIKE :search
            )
        """
        params["search"] = f"%{search.lower()}%"

    count_sql = f"""
        SELECT COUNT(DISTINCT p."ProductId")
        FROM twam."Products" p
        WHERE (p."DeletedInd" = false OR p."DeletedInd" IS NULL)
          AND p."IsActive" = true
          {search_clause}
    """
    total = db.execute(text(count_sql), params).scalar() or 0

    sql = f"""
        SELECT
            p."ProductId",
            p."ProductCode",
            p."Name",
            (
                SELECT pi."FilePath"
                FROM twam."ProductImage" pi
                WHERE pi."ProductId" = p."ProductId"
                  AND (pi."DeletedInd" = false OR pi."DeletedInd" IS NULL)
                  AND pi."FilePath" IS NOT NULL
                ORDER BY pi."ProductImageId" ASC
                LIMIT 1
            ) AS image
        FROM twam."Products" p
        WHERE (p."DeletedInd" = false OR p."DeletedInd" IS NULL)
          AND p."IsActive" = true
          {search_clause}
        ORDER BY p."ProductId" DESC
        LIMIT :limit OFFSET :offset
    """
    rows = db.execute(text(sql), params).fetchall()

    return {
        "total": int(total),
        "page": page,
        "size": size,
        "items": [
            {
                "productId":   r[0],
                "productCode": r[1],
                "name":        r[2],
                "image":       r[3],
            }
            for r in rows
        ],
    }


def get_variants_for_product(db: Session, product_id: int) -> list:
    sql = """
        SELECT
            pv."ProductVariantId",
            pv."ProductId",
            pv."VariantName",
            pv."Color",
            pv."IsCupSize",
            pv."IsActive",
            (
                SELECT pi."FilePath"
                FROM twam."ProductImage" pi
                WHERE pi."ProductVariantId" = pv."ProductVariantId"
                  AND (pi."DeletedInd" = false OR pi."DeletedInd" IS NULL)
                  AND pi."FilePath" IS NOT NULL
                ORDER BY pi."ProductImageId" ASC
                LIMIT 1
            ) AS image
        FROM twam."ProductVariants" pv
        WHERE pv."ProductId" = :pid
          AND (pv."DeletedInd" = false OR pv."DeletedInd" IS NULL)
        ORDER BY pv."ProductVariantId" ASC
    """
    rows = db.execute(text(sql), {"pid": product_id}).fetchall()
    return [
        {
            "productVariantId": r[0],
            "productId":        r[1],
            "variantName":      r[2],
            "color":            r[3],
            "isCupSize":        r[4],
            "isActive":         r[5],
            "image":            r[6],
        }
        for r in rows
    ]


def get_pvds_for_variant(db: Session, variant_id: int) -> list:
    sql = """
        SELECT
            pvd."ProductVariantDetailId",
            pvd."ProductId",
            pvd."ProductVariantId",
            pvd."ProductCode",
            pvd."StockQuantity",
            pvd."ProcessedQuantity",
            pvd."ReturnedQuantity",
            pvd."AvailableQuantity",
            pvd."AmendmentQuantity",
            pvd."StockId",
            pvd."IsLowStock",
            pvd."IsOutOfStock",
            pvd."MRPPrice",
            pvd."FinalPrice",
            pvd."CGST",
            pvd."SGST",
            pvd."IsActive",
            pvd."CreatedDate",
            pvd."ModifiedDate",
            s."SizeLabel",
            cs."SizeLabel" AS cup_size_label,
            pvd."Size" AS size_id,
            pvd."CupSize" AS cup_size_id
        FROM twam."ProductVariantDetail" pvd
        LEFT JOIN mdm."Size" s ON s."SizeId" = pvd."Size"
        LEFT JOIN mdm."CupSize" cs ON cs."CupSizeId" = pvd."CupSize"
        WHERE pvd."ProductVariantId" = :vid
          AND (pvd."DeletedInd" = false OR pvd."DeletedInd" IS NULL)
        ORDER BY pvd."ProductVariantDetailId" ASC
    """
    rows = db.execute(text(sql), {"vid": variant_id}).fetchall()
    return [
        {
            "productVariantDetailId": r[0],
            "productId":              r[1],
            "productVariantId":       r[2],
            "productCode":            r[3],
            "stockQuantity":          r[4],
            "processedQuantity":      r[5],
            "returnedQuantity":       r[6],
            "availableQuantity":      r[7],
            "amendmentQuantity":      r[8],
            "stockId":                r[9],
            "isLowStock":             r[10],
            "isOutOfStock":           r[11],
            "mrpPrice":               float(r[12]) if r[12] is not None else None,
            "finalPrice":             float(r[13]) if r[13] is not None else None,
            "cgst":                   float(r[14]) if r[14] is not None else None,
            "sgst":                   float(r[15]) if r[15] is not None else None,
            "isActive":               r[16],
            "createdDate":            r[17].isoformat() if r[17] else None,
            "modifiedDate":           r[18].isoformat() if r[18] else None,
            "sizeLabel":              r[19],
            "cupSizeLabel":           r[20],
            "sizeId":                 r[21],
            "cupSizeId":              r[22],
        }
        for r in rows
    ]