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
    """
    Update stock from the Stock Page. changedBy defaults to 'Admin'.
    This is the AUTHORITATIVE path for manual stock changes.
    """
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

    # changedBy = "Admin" for manual Stock Page edits
    effective_changed_by = changed_by or "Admin"

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
            changedBy              = effective_changed_by,
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


# ─── DB Change Detection ──────────────────────────────────────────────────────

def detect_and_log_db_changes(db: Session, pvd: ProductVariantDetail) -> bool:
    """
    Compare the current DB values against what was last logged in StockLog.
    If they differ, it means the DB was changed directly (not through our API).
    Creates a DB_CHANGE log entry and returns True when a discrepancy is found.

    Called automatically when the Stock Page loads PVDs.
    """
    pvd_id = pvd.productVariantDetailId

    # Get the most recent StockLog entry for this PVD
    # (any type — order, admin, productdetail, etc.)
    last_log = (
        db.query(StockLog)
        .filter(StockLog.productVariantDetailId == pvd_id)
        .order_by(StockLog.createdDate.desc())
        .first()
    )

    # No log at all — nothing to compare against
    if last_log is None:
        return False

    current_total     = pvd.stockQuantity     or 0
    current_processed = pvd.processedQuantity or 0
    current_returned  = pvd.returnedQuantity  or 0

    logged_total      = last_log.totalStock        or 0
    logged_processed  = last_log.processedQuantity or 0
    logged_returned   = last_log.returnedQuantity  or 0

    # Everything matches — no DB change
    if (current_total     == logged_total and
        current_processed == logged_processed and
        current_returned  == logged_returned):
        return False

    # Build a human-readable description of what changed
    diffs = []
    if current_total != logged_total:
        diffs.append(f"Total: {logged_total} → {current_total}")
    if current_processed != logged_processed:
        diffs.append(f"Processed: {logged_processed} → {current_processed}")
    if current_returned != logged_returned:
        diffs.append(f"Returned: {logged_returned} → {current_returned}")

    # Log the discrepancy as a DB_CHANGE entry
    db.add(StockLog(
        productVariantDetailId = pvd_id,
        productId              = pvd.productId,
        productVariantId       = pvd.productVariantId,
        stockId                = pvd.stockId,
        changeType             = "DB_CHANGE",
        previousValue          = logged_total,
        newValue               = current_total,
        totalStock             = current_total,
        processedQuantity      = current_processed,
        returnedQuantity       = current_returned,
        availableQuantity      = pvd.availableQuantity or 0,
        note                   = f"⚠️ Direct DB change detected: {'; '.join(diffs)}",
        changedBy              = "db_direct",
    ))
    db.commit()
    return True


# ─── Backfill: seed StockLog from existing ProductVariantDetail rows ──────────

def backfill_initial_stock(db: Session) -> dict:
    """
    For every active ProductVariantDetail that has NO entry in StockLog yet,
    insert a single INITIAL_STOCK log row using the current stock values.
    Skips PVDs that already have at least one log entry.
    Returns counts: { seeded, skipped, total }.
    Called automatically when the Stock Page loads — not shown in history UI.
    """
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

    seeded  = 0
    skipped = 0

    for pvd in pvds:
        if pvd.productVariantDetailId in existing_pvd_ids:
            skipped += 1
            continue

        total     = pvd.stockQuantity     or 0
        processed = pvd.processedQuantity or 0
        returned  = pvd.returnedQuantity  or 0
        available = pvd.availableQuantity if pvd.availableQuantity is not None \
                    else max(0, total - processed + returned)

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
        # Search by product Name, ProductCode, OR any matching StockId in PVDs
        search_clause = """
            AND (
                LOWER(p."Name") LIKE :search
                OR LOWER(p."ProductCode") LIKE :search
                OR EXISTS (
                    SELECT 1 FROM twam."ProductVariantDetail" pvd_s
                    WHERE pvd_s."ProductId" = p."ProductId"
                      AND (pvd_s."DeletedInd" = false OR pvd_s."DeletedInd" IS NULL)
                      AND LOWER(pvd_s."StockId") LIKE :search
                )
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
    """
    Fetch all PVDs for a variant, run DB change detection on each,
    and return them with a dbChangeAlert flag.
    """
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

    result = []
    db_change_pvd_ids = set()

    for r in rows:
        pvd_id = r[0]

        # ── Run DB change detection against this PVD ──────────────────────────
        pvd_obj = db.query(ProductVariantDetail).filter(
            ProductVariantDetail.productVariantDetailId == pvd_id
        ).first()
        db_change_detected = False
        if pvd_obj:
            try:
                db_change_detected = detect_and_log_db_changes(db, pvd_obj)
            except Exception:
                pass  # Never fail the page load because of detection

        if db_change_detected:
            db_change_pvd_ids.add(pvd_id)

        result.append({
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
            # ── DB change alert flag ──────────────────────────────────────────
            "dbChangeAlert":          db_change_detected,
        })

    return result


# ─── ProductAuditLog helpers (folded into stocklog — no new module needed) ────

from .models import ProductAuditLog as _PAL


def add_audit_event(
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
    Insert one product lifecycle audit event.
    Always wrapped in try/except at call sites so it never blocks operations.
    """
    db.add(_PAL(
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
    ))
    db.commit()


def get_product_audit_history(db: Session, product_id: int, limit: int = 500) -> list:
    """
    Full merged history for one product — newest first.
    Works for ALL products, including those created before audit logging was introduced,
    by deriving lifecycle events directly from source tables (Products, ProductVariants, PVDs).
    Combines:
      • Products / ProductVariants / ProductVariantDetail  (retroactive creation events)
      • ProductAuditLog   (admin-triggered structural changes after deploy)
      • StockLog          (every stock change, enriched with variant/size names)
    """
    events: list = []

    # ─── 1. Retroactive product creation (from Products table) ────────────────
    prod_row = db.execute(text("""
        SELECT "ProductId", "Name", "ProductCode", "CreatedDate", "CreatedBy"
        FROM twam."Products" WHERE "ProductId" = :pid
    """), {"pid": product_id}).fetchone()
    if prod_row:
        events.append({
            "eventId": f"product-{product_id}",
            "eventCategory": "lifecycle",
            "eventType": "PRODUCT_CREATED",
            "productId": product_id,
            "productVariantId": None, "productVariantDetailId": None,
            "productCode": prod_row[2],
            "productName": prod_row[1],
            "variantName": None, "sizeLabel": None, "stockId": None,
            "description": f"Product created: {prod_row[1] or ''} [{prod_row[2] or ''}]",
            "changedBy": prod_row[4] or "system",
            "eventSource": "System",
            "stockBefore": None, "stockAfter": None, "changeType": None,
            "previousValue": None, "newValue": None,
            "totalStock": None, "processedQuantity": None,
            "returnedQuantity": None, "availableQuantity": None, "note": None,
            "createdDate": prod_row[3].isoformat() if prod_row[3] else None,
        })

    # ─── 2. Retroactive variant creation (from ProductVariants table) ─────────
    variant_rows = db.execute(text("""
        SELECT "ProductVariantId","VariantName","Color","IsCupSize",
               "CreatedDate","CreatedBy","IsActive","DeletedInd"
        FROM twam."ProductVariants"
        WHERE "ProductId" = :pid
        ORDER BY "CreatedDate" ASC NULLS LAST
    """), {"pid": product_id}).fetchall()
    for v in variant_rows:
        v_id, v_name, v_color, v_cup, v_date, v_by, v_active, v_del = v
        label = f"{v_name or ''}{' / ' + v_color if v_color else ''}"
        cup_note = " (cup-size enabled)" if v_cup else ""
        is_del = bool(v_del)
        events.append({
            "eventId": f"variant-{v_id}",
            "eventCategory": "lifecycle",
            "eventType": "VARIANT_DELETED" if is_del else "VARIANT_ADDED",
            "productId": product_id,
            "productVariantId": v_id, "productVariantDetailId": None,
            "productCode": None, "productName": None,
            "variantName": v_name or v_color,
            "sizeLabel": None, "stockId": None,
            "description": f"Variant {'deleted' if is_del else 'added'}: {label}{cup_note}",
            "changedBy": v_by or "system",
            "eventSource": "ProductDetail",
            "stockBefore": None, "stockAfter": None, "changeType": None,
            "previousValue": None, "newValue": None,
            "totalStock": None, "processedQuantity": None,
            "returnedQuantity": None, "availableQuantity": None, "note": None,
            "createdDate": v_date.isoformat() if v_date else None,
        })

    # ─── 3. Retroactive size/PVD creation (from ProductVariantDetail table) ───
    pvd_rows = db.execute(text("""
        SELECT pvd."ProductVariantDetailId", pvd."ProductVariantId",
               pvd."StockQuantity", pvd."ProcessedQuantity",
               pvd."ReturnedQuantity", pvd."AvailableQuantity",
               pvd."StockId", pvd."CreatedDate", pvd."CreatedBy",
               pvd."DeletedInd",
               COALESCE(sz."SizeLabel",'')  AS size_label,
               COALESCE(cs."SizeLabel",'')  AS cup_label,
               pv."VariantName", pv."Color"
        FROM twam."ProductVariantDetail" pvd
        LEFT JOIN twam."ProductVariants"  pv ON pv."ProductVariantId"  = pvd."ProductVariantId"
        LEFT JOIN mdm."Size"    sz ON sz."SizeId"    = pvd."Size"
        LEFT JOIN mdm."CupSize" cs ON cs."CupSizeId" = pvd."CupSize"
        WHERE pvd."ProductId" = :pid
        ORDER BY pvd."CreatedDate" ASC NULLS LAST
    """), {"pid": product_id}).fetchall()
    for row in pvd_rows:
        (pvd_id, pv_id, stk, proc, ret, avail,
         s_id, s_date, s_by, s_del,
         size_lbl, cup_lbl, vname, vcolor) = row
        size_display = " / ".join(p for p in [size_lbl, cup_lbl] if p) or "—"
        variant_display = vname or vcolor or f"Variant {pv_id}"
        total = stk or 0
        is_del = bool(s_del)
        desc = (f"Size {'deleted' if is_del else 'added'}: {size_display} "
                f"in {variant_display} — initial stock: {total}")
        events.append({
            "eventId": f"pvd-{pvd_id}",
            "eventCategory": "lifecycle",
            "eventType": "SIZE_DELETED" if is_del else "SIZE_ADDED",
            "productId": product_id,
            "productVariantId": pv_id, "productVariantDetailId": pvd_id,
            "productCode": None, "productName": None,
            "variantName": variant_display,
            "sizeLabel": size_display,
            "stockId": s_id,
            "description": desc,
            "changedBy": s_by or "system",
            "eventSource": "ProductDetail",
            "stockBefore": None, "stockAfter": total, "changeType": None,
            "previousValue": None, "newValue": total,
            "totalStock": total, "processedQuantity": proc or 0,
            "returnedQuantity": ret or 0,
            "availableQuantity": avail or max(0, total - (proc or 0) + (ret or 0)),
            "note": f"Initial stock: {total}",
            "createdDate": s_date.isoformat() if s_date else None,
        })

    # ─── 4. ProductAuditLog events (admin-triggered after deploy) ─────────────
    # De-duplicate: skip lifecycle event types that are already derived above
    DERIVED_TYPES = {"PRODUCT_CREATED", "VARIANT_ADDED", "VARIANT_DELETED", "SIZE_ADDED", "SIZE_DELETED"}
    audit_rows = (
        db.query(_PAL)
        .filter(_PAL.productId == product_id)
        .all()
    )
    derived_event_ids = {e["eventId"] for e in events}
    for r in audit_rows:
        # Only add UPDATES from audit log — creations already derived from source tables
        if r.eventType not in DERIVED_TYPES:
            events.append({
                "eventId": f"audit-{r.productAuditLogId}",
                "eventCategory": "lifecycle",
                "eventType": r.eventType,
                "productId": r.productId,
                "productVariantId": r.productVariantId,
                "productVariantDetailId": r.productVariantDetailId,
                "productCode": r.productCode, "productName": r.productName,
                "variantName": r.variantName, "sizeLabel": r.sizeLabel,
                "stockId": r.stockId, "description": r.description,
                "changedBy": r.changedBy, "eventSource": r.eventSource,
                "stockBefore": r.stockBefore, "stockAfter": r.stockAfter,
                "changeType": None, "previousValue": None, "newValue": None,
                "totalStock": None, "processedQuantity": None,
                "returnedQuantity": None, "availableQuantity": None, "note": None,
                "createdDate": r.createdDate.isoformat() if r.createdDate else None,
            })

    # ─── 5. Stock events — enriched with variant / size labels via JOINs ──────
    stock_sql = text("""
        SELECT
            sl."StockLogId",
            sl."ProductVariantDetailId",
            sl."ProductVariantId",
            sl."ProductId",
            sl."StockId",
            sl."ChangeType",
            sl."PreviousValue",
            sl."NewValue",
            sl."TotalStock",
            sl."ProcessedQuantity",
            sl."ReturnedQuantity",
            sl."AvailableQuantity",
            sl."Note",
            sl."ChangedBy",
            sl."CreatedDate",
            pv."VariantName",
            pv."Color",
            COALESCE(sz."SizeLabel", '')  AS size_label,
            COALESCE(cs."SizeLabel", '')  AS cup_size_label
        FROM twam."StockLog" sl
        LEFT JOIN twam."ProductVariants"      pv  ON pv."ProductVariantId"  = sl."ProductVariantId"
        LEFT JOIN twam."ProductVariantDetail" pvd ON pvd."ProductVariantDetailId" = sl."ProductVariantDetailId"
        LEFT JOIN mdm."Size"    sz ON sz."SizeId"    = pvd."Size"
        LEFT JOIN mdm."CupSize" cs ON cs."CupSizeId" = pvd."CupSize"
        WHERE sl."ProductId" = :pid
    """)
    stock_rows = db.execute(stock_sql, {"pid": product_id}).mappings().fetchall()

    # Serialize OLD audit_rows (now de-duped — only non-derived types above)
    for r in audit_rows:
        events.append({
            "eventId":               f"audit-{r.productAuditLogId}",
            "eventCategory":         "lifecycle",
            "eventType":             r.eventType,
            "productId":             r.productId,
            "productVariantId":      r.productVariantId,
            "productVariantDetailId":r.productVariantDetailId,
            "productCode":           r.productCode,
            "productName":           r.productName,
            "variantName":           r.variantName,
            "sizeLabel":             r.sizeLabel,
            "stockId":               r.stockId,
            "description":           r.description,
            "changedBy":             r.changedBy,
            "eventSource":           r.eventSource,
            "stockBefore":           r.stockBefore,
            "stockAfter":            r.stockAfter,
            "changeType":            None,
            "previousValue":         None,
            "newValue":              None,
            "totalStock":            None,
            "processedQuantity":     None,
            "returnedQuantity":      None,
            "availableQuantity":     None,
            "note":                  None,
            "createdDate":           r.createdDate.isoformat() if r.createdDate else None,
        })

    # Serialize stock events
    for r in stock_rows:
        size_parts = [r["size_label"], r["cup_size_label"]]
        size_display = " / ".join(p for p in size_parts if p) or None
        variant_display = r["VariantName"] or None

        events.append({
            "eventId":               f"stock-{r['StockLogId']}",
            "eventCategory":         "stock",
            "eventType":             r["ChangeType"],
            "productId":             r["ProductId"],
            "productVariantId":      r["ProductVariantId"],
            "productVariantDetailId":r["ProductVariantDetailId"],
            "productCode":           None,
            "productName":           None,
            "variantName":           variant_display,
            "sizeLabel":             size_display,
            "stockId":               r["StockId"],
            "description":           r["Note"] or r["ChangeType"],
            "changedBy":             r["ChangedBy"],
            "eventSource":           r["ChangedBy"],
            "stockBefore":           r["PreviousValue"],
            "stockAfter":            r["NewValue"],
            "changeType":            r["ChangeType"],
            "previousValue":         r["PreviousValue"],
            "newValue":              r["NewValue"],
            "totalStock":            r["TotalStock"],
            "processedQuantity":     r["ProcessedQuantity"],
            "returnedQuantity":      r["ReturnedQuantity"],
            "availableQuantity":     r["AvailableQuantity"],
            "note":                  r["Note"],
            "createdDate":           r["CreatedDate"].isoformat() if r["CreatedDate"] else None,
        })

    events.sort(key=lambda x: x["createdDate"] or "", reverse=True)
    return events[:limit]


def get_all_audit_history(db: Session, page: int = 1, size: int = 50) -> dict:
    """
    Paginated global history — ProductAuditLog UNION StockLog, newest first.
    Uses a SQL UNION for proper server-side pagination across both tables.
    """
    offset = (page - 1) * size

    count_sql = text("""
        SELECT
          (SELECT COUNT(*) FROM twam."ProductAuditLog") +
          (SELECT COUNT(*) FROM twam."StockLog") AS total
    """)
    total = db.execute(count_sql).scalar() or 0

    union_sql = text("""
        SELECT
            'lifecycle'                       AS event_category,
            CAST("ProductAuditLogId" AS TEXT) AS event_id_raw,
            "EventType"                       AS event_type,
            "ProductId"                       AS product_id,
            "ProductVariantId"                AS product_variant_id,
            "ProductVariantDetailId"          AS product_variant_detail_id,
            "ProductCode"                     AS product_code,
            "ProductName"                     AS product_name,
            "EventSource"                     AS event_source,
            "Description"                     AS description,
            "ChangedBy"                       AS changed_by,
            "VariantName"                     AS variant_name,
            "SizeLabel"                       AS size_label,
            "StockId"                         AS stock_id,
            "StockBefore"                     AS stock_before,
            "StockAfter"                      AS stock_after,
            NULL::INTEGER                     AS previous_value,
            NULL::INTEGER                     AS new_value,
            NULL::INTEGER                     AS total_stock,
            NULL::INTEGER                     AS processed_quantity,
            NULL::INTEGER                     AS returned_quantity,
            NULL::INTEGER                     AS available_quantity,
            NULL::TEXT                        AS note,
            "CreatedDate"                     AS created_date
        FROM twam."ProductAuditLog"

        UNION ALL

        SELECT
            'stock'                           AS event_category,
            CAST("StockLogId" AS TEXT)        AS event_id_raw,
            "ChangeType"                      AS event_type,
            "ProductId"                       AS product_id,
            "ProductVariantId"                AS product_variant_id,
            "ProductVariantDetailId"          AS product_variant_detail_id,
            NULL                              AS product_code,
            NULL                              AS product_name,
            "ChangedBy"                       AS event_source,
            "Note"                            AS description,
            "ChangedBy"                       AS changed_by,
            NULL                              AS variant_name,
            NULL                              AS size_label,
            "StockId"                         AS stock_id,
            "PreviousValue"                   AS stock_before,
            "NewValue"                        AS stock_after,
            "PreviousValue"                   AS previous_value,
            "NewValue"                        AS new_value,
            "TotalStock"                      AS total_stock,
            "ProcessedQuantity"               AS processed_quantity,
            "ReturnedQuantity"                AS returned_quantity,
            "AvailableQuantity"               AS available_quantity,
            "Note"                            AS note,
            "CreatedDate"                     AS created_date
        FROM twam."StockLog"

        ORDER BY created_date DESC NULLS LAST
        LIMIT :lim OFFSET :off
    """)
    rows = db.execute(union_sql, {"lim": size, "off": offset}).mappings().fetchall()

    items = []
    for r in rows:
        items.append({
            "eventId":               f"{r['event_category']}-{r['event_id_raw']}",
            "eventCategory":         r["event_category"],
            "eventType":             r["event_type"],
            "productId":             r["product_id"],
            "productVariantId":      r["product_variant_id"],
            "productVariantDetailId":r["product_variant_detail_id"],
            "productCode":           r["product_code"],
            "productName":           r["product_name"],
            "variantName":           r["variant_name"],
            "sizeLabel":             r["size_label"],
            "stockId":               r["stock_id"],
            "description":           r["description"],
            "changedBy":             r["changed_by"],
            "eventSource":           r["event_source"],
            "stockBefore":           r["stock_before"],
            "stockAfter":            r["stock_after"],
            "previousValue":         r["previous_value"],
            "newValue":              r["new_value"],
            "totalStock":            r["total_stock"],
            "processedQuantity":     r["processed_quantity"],
            "returnedQuantity":      r["returned_quantity"],
            "availableQuantity":     r["available_quantity"],
            "note":                  r["note"],
            "createdDate":           r["created_date"].isoformat() if r["created_date"] else None,
        })

    return {"total": total, "page": page, "size": size, "items": items}


def _serialize_audit(r: "_PAL") -> dict:
    """Legacy — kept for any direct callers."""
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
