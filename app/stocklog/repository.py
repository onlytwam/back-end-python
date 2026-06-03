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
                "image":       resolve_url(r[3]) or "",
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
            "image":            resolve_url(r[6]) or "",
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
            "sizeLabel":              _fmt_size(r[19] or "", r[20] or "") or (r[19] or ""),
            "cupSizeLabel":           "",  # merged into sizeLabel
            "sizeId":                 r[21],
            "cupSizeId":              r[22],
            # ── DB change alert flag ──────────────────────────────────────────
            "dbChangeAlert":          db_change_detected,
        })

    return result


# ─── ProductAuditLog helpers (folded into stocklog — no new module needed) ────

from .models import ProductAuditLog as _PAL
from app.shared.url_helpers import resolve_url

def _fmt_size(band: str, cup: str) -> str:
    """Combine band and cup size into a single display string like '32B'.
    Falls back gracefully if either part is missing."""
    b = (band or "").strip()
    c = (cup  or "").strip()
    if b and c:
        return f"{b}{c}"   # e.g. "32B"
    return b or c or ""



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
        size_display = _fmt_size(size_lbl, cup_lbl) or "—"
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

    # Serialize stock events — skip INITIAL_STOCK (seeded bootstrap data)
    for r in stock_rows:
        # Skip auto-seeded initial stock entries — not meaningful history
        if r["ChangeType"] == "INITIAL_STOCK":
            continue

        size_display = _fmt_size(r["size_label"], r["cup_size_label"]) or None
        variant_display = r["VariantName"] or None

        # DB_CHANGE — treat as an Admin stock correction, not a system alert
        change_type = r["ChangeType"]
        changed_by  = r["ChangedBy"]
        note        = r["Note"]
        if change_type == "DB_CHANGE":
            change_type = "TOTAL_STOCK"
            changed_by  = changed_by if (changed_by and changed_by not in ("db_direct", "system")) else "Admin"
            note        = (note or "").replace("⚠️ Direct DB change detected:", "Stock corrected:").strip() or "Stock corrected"

        events.append({
            "eventId":               f"stock-{r['StockLogId']}",
            "eventCategory":         "stock",
            "eventType":             change_type,
            "productId":             r["ProductId"],
            "productVariantId":      r["ProductVariantId"],
            "productVariantDetailId":r["ProductVariantDetailId"],
            "productCode":           None,
            "productName":           None,
            "variantName":           variant_display,
            "sizeLabel":             size_display,
            "stockId":               r["StockId"],
            "description":           note or change_type,
            "changedBy":             changed_by,
            "eventSource":           changed_by,
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

    # ─── 6. Role lookup — enrich every event with the changedBy user role ──────
    unique_users = list({e["changedBy"] for e in events if e.get("changedBy") and e["changedBy"] not in ("system", "db_direct", "Admin", "order_system")})
    role_map: dict = {}
    if unique_users:
        in_params = {f"u{i}": v for i, v in enumerate(unique_users)}
        in_clause = ", ".join(f":u{i}" for i in range(len(unique_users)))
        role_rows = db.execute(text(f"""
            SELECT iu."UserName", ur."RoleName"
            FROM auth."IdentityUser" iu
            LEFT JOIN twam."UserRole" ur ON ur."UserRoleId" = iu."UserRoleId"
            WHERE iu."UserName" IN ({in_clause})
        """), in_params).fetchall()
        for username, rolename in role_rows:
            role_map[username] = rolename or "Admin"
    role_map["system"]       = "System"
    role_map["db_direct"]    = "Direct DB"
    role_map["Admin"]        = "Admin"
    role_map["order_system"] = "Order System"
    for e in events:
        cb = e.get("changedBy") or ""
        e["role"] = role_map.get(cb, "")

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


def get_product_directory(db: Session, page: int = 1, size: int = 50, search: str = "") -> dict:
    """
    Product Directory for the global History modal.
    Returns each product with:
      - name, code, image
      - creation: date, day, time, by (username + role)
      - modifications: list of {date, what changed (VARIANT_ADDED/VARIANT_DETAIL_MODIFIED/etc), changedBy}
    Only PRODUCT_CREATED, VARIANT_ADDED, VARIANT_DELETED, SIZE_ADDED, SIZE_DELETED from
    ProductAuditLog count as "modifications" (not stock changes).
    """
    from sqlalchemy import text

    offset = (page - 1) * size

    search_clause = ""
    params: dict = {"lim": size, "off": offset}
    if search.strip():
        search_clause = """
            AND (
                p."Name"        ILIKE :q
                OR p."ProductCode" ILIKE :q
            )
        """
        params["q"] = f"%{search.strip()}%"

    count_sql = text(f"""
        SELECT COUNT(*) FROM twam."Products" p
        WHERE (p."DeletedInd" IS DISTINCT FROM TRUE)
        {search_clause}
    """)
    total = db.execute(count_sql, params).scalar() or 0

    product_sql = text(f"""
        SELECT
            p."ProductId",
            p."Name",
            p."ProductCode",
            p."CreatedDate",
            p."CreatedBy",
            (
                SELECT pi."FilePath"
                FROM twam."ProductImage" pi
                WHERE pi."ProductId" = p."ProductId"
                  AND (pi."DeletedInd" IS DISTINCT FROM TRUE)
                  AND pi."FilePath" IS NOT NULL
                ORDER BY pi."ProductImageId" ASC
                LIMIT 1
            ) AS image
        FROM twam."Products" p
        WHERE (p."DeletedInd" IS DISTINCT FROM TRUE)
        {search_clause}
        ORDER BY p."CreatedDate" DESC NULLS LAST
        LIMIT :lim OFFSET :off
    """)
    products = db.execute(product_sql, params).fetchall()

    if not products:
        return {"items": [], "total": total, "page": page, "size": size}

    product_ids = [r[0] for r in products]

    # Fetch creator role info: join IdentityUser -> UserRole
    creator_usernames = list({r[4] for r in products if r[4]})
    role_map: dict = {}
    if creator_usernames:
        # Build parameterized IN clause
        in_params = {f"u{i}": v for i, v in enumerate(creator_usernames)}
        in_clause = ", ".join(f":u{i}" for i in range(len(creator_usernames)))
        role_sql = text(f"""
            SELECT iu."UserName", ur."RoleName"
            FROM auth."IdentityUser" iu
            LEFT JOIN twam."UserRole" ur ON ur."UserRoleId" = iu."UserRoleId"
            WHERE iu."UserName" IN ({in_clause})
        """)
        role_rows = db.execute(role_sql, in_params).fetchall()
        for username, rolename in role_rows:
            role_map[username] = rolename or "Admin"

    # Fetch modifications from ProductAuditLog (structural events only, not stock)
    structural_types = (
        "PRODUCT_UPDATED",
        "VARIANT_ADDED", "VARIANT_DELETED",
        "SIZE_ADDED", "SIZE_DELETED",
    )
    if product_ids:
        in_pids = {f"p{i}": v for i, v in enumerate(product_ids)}
        in_clause2 = ", ".join(f":p{i}" for i in range(len(product_ids)))
        in_types = {f"t{i}": v for i, v in enumerate(structural_types)}
        in_clause3 = ", ".join(f":t{i}" for i in range(len(structural_types)))

        mod_sql = text(f"""
            SELECT
                pal."ProductId",
                pal."EventType",
                pal."ChangedBy",
                pal."VariantName",
                COALESCE(sz."SizeLabel", '') AS band_label,
                COALESCE(cs."SizeLabel", '') AS cup_label,
                pal."Description",
                pal."CreatedDate",
                pal."StockAfter"
            FROM twam."ProductAuditLog" pal
            LEFT JOIN twam."ProductVariantDetail" pvd ON pvd."ProductVariantDetailId" = pal."ProductVariantDetailId"
            LEFT JOIN mdm."Size"    sz ON sz."SizeId"    = pvd."Size"
            LEFT JOIN mdm."CupSize" cs ON cs."CupSizeId" = pvd."CupSize"
            WHERE pal."ProductId" IN ({in_clause2})
              AND pal."EventType" IN ({in_clause3})
            ORDER BY pal."CreatedDate" DESC NULLS LAST
        """)
        mod_params = {**in_pids, **in_types}
        mod_rows = db.execute(mod_sql, mod_params).fetchall()
    else:
        mod_rows = []

    # Group modifications by product
    from collections import defaultdict
    mods_by_product: dict = defaultdict(list)
    for pid, etype, changed_by, variant_name, band_label, cup_label, description, created_date, stock_after in mod_rows:
        # Human-friendly label
        label_map = {
            "PRODUCT_UPDATED":  "Product details modified",
            "VARIANT_ADDED":    "Variant added",
            "VARIANT_DELETED":  "Variant deleted",
            "SIZE_ADDED":       "Variant detail added",
            "SIZE_DELETED":     "Variant detail deleted",
        }
        label = label_map.get(etype, etype.replace("_", " ").title())
        context = ""
        size_display = _fmt_size(band_label, cup_label)
        if variant_name:
            context = variant_name
            if size_display:
                context += f" / {size_display}"
        mods_by_product[pid].append({
            "eventType":   etype,
            "label":       label,
            "context":     context,
            "changedBy":   changed_by,
            "role":        role_map.get(changed_by, ""),
            "createdDate": created_date.isoformat() if created_date else None,
            "stockAfter":  int(stock_after) if stock_after is not None else None,
            "sizeDisplay": _fmt_size(band_label, cup_label),
        })

    items = []
    for row in products:
        pid, name, code, created_date, created_by, image = row
        items.append({
            "productId":     pid,
            "productName":   name,
            "productCode":   code,
            "image":         resolve_url(image) or "",
            "createdDate":   created_date.isoformat() if created_date else None,
            "createdBy":     created_by,
            "createdByRole": role_map.get(created_by or "", ""),
            "modifications": mods_by_product.get(pid, []),
        })

    return {"items": items, "total": total, "page": page, "size": size}

# ─── Excel Export helpers ──────────────────────────────────────────────────────

def export_changes_excel(db: Session) -> bytes:
    """
    Hierarchical Excel — Product > Variant > Sizes.
    Colors: Burgundy #6B0F2A, Warm Ivory #F5F0E6, White, Black text.
    No level-marker column.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from io import BytesIO
    from collections import OrderedDict

    rows = db.execute(text("""
        SELECT
            pal."CreatedDate",
            pal."ProductCode",
            pal."ProductName",
            pal."EventType",
            pal."VariantName",
            COALESCE(sz."SizeLabel", '') AS band_label,
            COALESCE(cs."SizeLabel", '') AS cup_label,
            pal."StockAfter",
            pal."ChangedBy",
            COALESCE(ur."RoleName", pal."ChangedBy", 'system') AS role
        FROM twam."ProductAuditLog" pal
        LEFT JOIN twam."ProductVariantDetail" pvd ON pvd."ProductVariantDetailId" = pal."ProductVariantDetailId"
        LEFT JOIN mdm."Size"    sz ON sz."SizeId"    = pvd."Size"
        LEFT JOIN mdm."CupSize" cs ON cs."CupSizeId" = pvd."CupSize"
        LEFT JOIN auth."IdentityUser" iu ON iu."UserName" = pal."ChangedBy"
        LEFT JOIN twam."UserRole"     ur ON ur."UserRoleId" = iu."UserRoleId"
        WHERE pal."EventType" IN (
            'PRODUCT_CREATED','PRODUCT_UPDATED','PRODUCT_DELETED',
            'VARIANT_ADDED','VARIANT_DELETED',
            'SIZE_ADDED','SIZE_DELETED'
        )
        ORDER BY pal."ProductCode" ASC, pal."VariantName" ASC NULLS FIRST, pal."CreatedDate" ASC NULLS LAST
    """)).fetchall()

    # ── Colors ───────────────────────────────────────────────────────────────
    BURGUNDY      = "6B0F2A"
    BURGUNDY_SOFT = "8C1A37"   # slightly lighter for variant header
    IVORY         = "F5F0E6"
    IVORY_ALT     = "EDE8DC"   # slightly darker ivory for alternating rows
    WHITE         = "FFFFFF"
    BLACK         = "000000"
    PURPLE_TEXT   = "5B0E7F"
    BLUE_TEXT     = "0A4F8A"
    GREEN_TEXT    = "1A5C1A"
    RED_TEXT      = "8B0000"

    # ── Styles ────────────────────────────────────────────────────────────────
    thin = Side(style="thin",   color="C8BEB4")
    med  = Side(style="medium", color=BURGUNDY)

    def fill(hex_color):
        return PatternFill("solid", fgColor=hex_color)

    def bord(left_side=None):
        s = left_side or thin
        return Border(left=s, right=thin, top=thin, bottom=thin)

    def font(bold=False, color=BLACK, size=10, italic=False):
        return Font(name="Arial", bold=bold, color=color, size=size, italic=italic)

    C  = Alignment(horizontal="center", vertical="center")
    L  = Alignment(horizontal="left",   vertical="center", wrap_text=True)
    LC = Alignment(horizontal="left",   vertical="center")

    EVENT_LABEL = {
        "PRODUCT_CREATED": "Product Created",
        "PRODUCT_UPDATED": "Product Updated",
        "PRODUCT_DELETED": "Product Deleted",
        "VARIANT_ADDED":   "Variant Added",
        "VARIANT_DELETED": "Variant Deleted",
        "SIZE_ADDED":      "Size Added",
        "SIZE_DELETED":    "Size Removed",
    }
    EVENT_COLOR = {
        "PRODUCT_CREATED": GREEN_TEXT,
        "PRODUCT_UPDATED": BLUE_TEXT,
        "PRODUCT_DELETED": RED_TEXT,
        "VARIANT_ADDED":   PURPLE_TEXT,
        "VARIANT_DELETED": RED_TEXT,
        "SIZE_ADDED":      BLUE_TEXT,
        "SIZE_DELETED":    RED_TEXT,
    }

    # ── Build hierarchy ───────────────────────────────────────────────────────
    products = OrderedDict()
    for r in rows:
        created_date, prod_code, prod_name, event_type, variant, band_lbl, cup_lbl, stock_after, changed_by, role = r
        key = prod_code or "(no code)"
        if key not in products:
            products[key] = {"name": prod_name or "", "prod_rows": [], "variants": OrderedDict()}
        p = products[key]
        if event_type in ("PRODUCT_CREATED", "PRODUCT_UPDATED", "PRODUCT_DELETED"):
            p["prod_rows"].append(r)
            continue
        vkey = variant or "(no variant)"
        if vkey not in p["variants"]:
            p["variants"][vkey] = {"variant_rows": [], "size_rows": []}
        v = p["variants"][vkey]
        if event_type in ("VARIANT_ADDED", "VARIANT_DELETED"):
            v["variant_rows"].append(r)
        else:
            v["size_rows"].append(r)

    # ── Workbook ──────────────────────────────────────────────────────────────
    wb = Workbook()
    ws = wb.active
    ws.title = "Product Changes"

    # Columns: Date | Time | Event | Detail / Variant | Size | Stock | Changed By | Role
    NCOLS      = 8
    HDR_COLS   = ["Date",  "Time", "Event",          "Detail / Variant",  "Size",  "Stock", "Changed By",  "Role"]
    COL_WIDTHS = [15,       9,      22,               34,                   12,      10,      22,             16   ]

    ws.row_dimensions[1].height = 30
    for ci, (h, w) in enumerate(zip(HDR_COLS, COL_WIDTHS), 1):
        c = ws.cell(row=1, column=ci, value=h)
        c.font      = Font(name="Arial", bold=True, color=WHITE, size=10)
        c.fill      = fill(BURGUNDY)
        c.alignment = C
        c.border    = bord(med if ci == 1 else thin)
        ws.column_dimensions[get_column_letter(ci)].width = w
    ws.freeze_panes = "A2"

    row_idx = 2
    total_records = len(rows)

    def write_merged_header(ri, text, bg, text_color, text_size=10, bold=True, height=22):
        ws.row_dimensions[ri].height = height
        for ci in range(1, NCOLS + 1):
            c = ws.cell(row=ri, column=ci, value="")
            c.fill   = fill(bg)
            c.border = bord(med if ci == 1 else thin)
        c0 = ws.cell(row=ri, column=1, value=text)
        c0.font      = Font(name="Arial", bold=bold, color=text_color, size=text_size)
        c0.fill      = fill(bg)
        c0.alignment = LC
        ws.merge_cells(start_row=ri, start_column=1, end_row=ri, end_column=NCOLS)

    def write_data_row(ri, date_str, time_str, event, detail, size, stock, by, role,
                       row_fill_hex, evt_color, bold=False):
        ws.row_dimensions[ri].height = 18
        values = [date_str, time_str, event, detail, size, stock, by, role]
        aligns = [C,         C,         C,     L,      C,     C,    LC,   C  ]
        for ci, (val, aln) in enumerate(zip(values, aligns), 1):
            c = ws.cell(row=ri, column=ci, value=val)
            c.font      = Font(name="Arial", bold=(bold and ci == 3), color=(evt_color if ci == 3 else BLACK), size=10)
            c.fill      = fill(row_fill_hex)
            c.alignment = aln
            c.border    = bord(med if ci == 1 else thin)

    def write_spacer(ri, height=5):
        ws.row_dimensions[ri].height = height
        for ci in range(1, NCOLS + 1):
            c = ws.cell(row=ri, column=ci, value="")
            c.fill   = fill(IVORY)
            c.border = bord(thin)

    for prod_code, pdata in products.items():
        prod_name = pdata["name"]

        # ══ PRODUCT header ══════════════════════════════════════════════════
        write_merged_header(row_idx,
            f"  {prod_code}   {prod_name}",
            bg=BURGUNDY, text_color=WHITE, text_size=11, height=26)
        row_idx += 1

        # ── Product Created/Updated/Deleted — FIRST row after product header ──
        for r in pdata["prod_rows"]:
            created_date, _, _, event_type, _, _, _, _, changed_by, role = r
            date_str = created_date.strftime("%d %b %Y") if created_date else ""
            time_str = created_date.strftime("%H:%M")     if created_date else ""
            write_data_row(row_idx,
                date_str, time_str,
                EVENT_LABEL.get(event_type, event_type),
                prod_name, "", "", changed_by or "", role or "",
                IVORY, EVENT_COLOR.get(event_type, BLACK), bold=True)
            row_idx += 1

        # ── Variants ─────────────────────────────────────────────────────────
        for vname, vdata in pdata["variants"].items():
            vname_clean = vname
            if vname_clean.startswith(prod_name):
                vname_clean = vname_clean[len(prod_name):].lstrip(" -–—")
            if not vname_clean or vname_clean == "(no variant)":
                vname_clean = vname

            # ▸ Variant sub-header
            write_merged_header(row_idx,
                f"      {vname_clean}",
                bg=IVORY_ALT, text_color=BURGUNDY, text_size=10, bold=True, height=20)
            row_idx += 1

            # Variant Added / Deleted rows
            for r in vdata["variant_rows"]:
                created_date, _, _, event_type, _, _, _, _, changed_by, role = r
                date_str = created_date.strftime("%d %b %Y") if created_date else ""
                time_str = created_date.strftime("%H:%M")     if created_date else ""
                write_data_row(row_idx,
                    date_str, time_str,
                    EVENT_LABEL.get(event_type, event_type),
                    vname_clean, "", "", changed_by or "", role or "",
                    IVORY, EVENT_COLOR.get(event_type, BLACK), bold=True)
                row_idx += 1

            # Size rows
            size_rows_sorted = sorted(vdata["size_rows"],
                key=lambda r: (str(r[5] or ""), str(r[0] or "")))
            for alt, r in enumerate(size_rows_sorted):
                created_date, _, _, event_type, _, band_lbl, cup_lbl, stock_after, changed_by, role = r
                date_str  = created_date.strftime("%d %b %Y") if created_date else ""
                time_str  = created_date.strftime("%H:%M")     if created_date else ""
                size_val  = _fmt_size(band_lbl, cup_lbl)
                stock_val = int(stock_after) if stock_after is not None else ""
                row_bg    = WHITE if alt % 2 == 0 else IVORY
                write_data_row(row_idx,
                    date_str, time_str,
                    EVENT_LABEL.get(event_type, event_type),
                    "", size_val, stock_val, changed_by or "", role or "",
                    row_bg, EVENT_COLOR.get(event_type, BLACK), bold=False)
                row_idx += 1

            write_spacer(row_idx)
            row_idx += 1

        # Gap between products
        write_spacer(row_idx, height=10)
        row_idx += 1

    # Summary row
    ws.row_dimensions[row_idx].height = 20
    c = ws.cell(row=row_idx, column=1,
                value=f"  Total: {total_records} records  ·  {len(products)} products")
    c.font      = Font(name="Arial", bold=True, size=10, color=BURGUNDY)
    c.fill      = fill(IVORY)
    c.alignment = LC
    ws.merge_cells(start_row=row_idx, start_column=1, end_row=row_idx, end_column=NCOLS)

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_stock_excel(db: Session) -> bytes:
    """
    Hierarchical Excel — identical template to Product Changes.
    Product header (Burgundy) > Variant sub-header (Ivory-alt) > Stock rows (White/Ivory alternating).
    Columns: Date | Time | Event | Size | Stock ID | Prev | New | Change | Changed By | Role
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from io import BytesIO
    from collections import OrderedDict

    rows = db.execute(text("""
        SELECT
            sl."CreatedDate",
            p."ProductCode",
            p."Name"               AS product_name,
            pv."VariantName",
            COALESCE(sz."SizeLabel", '') AS size_label,
            COALESCE(cs."SizeLabel", '') AS cup_size_label,
            sl."StockId",
            sl."PreviousValue",
            sl."NewValue",
            sl."ChangedBy",
            COALESCE(ur."RoleName", sl."ChangedBy", 'Admin') AS role
        FROM twam."StockLog" sl
        LEFT JOIN twam."Products"             p   ON p."ProductId"               = sl."ProductId"
        LEFT JOIN twam."ProductVariants"      pv  ON pv."ProductVariantId"       = sl."ProductVariantId"
        LEFT JOIN twam."ProductVariantDetail" pvd ON pvd."ProductVariantDetailId"= sl."ProductVariantDetailId"
        LEFT JOIN mdm."Size"    sz ON sz."SizeId"    = pvd."Size"
        LEFT JOIN mdm."CupSize" cs ON cs."CupSizeId" = pvd."CupSize"
        LEFT JOIN auth."IdentityUser" iu ON iu."UserName" = sl."ChangedBy"
        LEFT JOIN twam."UserRole"     ur ON ur."UserRoleId" = iu."UserRoleId"
        WHERE sl."ChangeType" = 'TOTAL_STOCK'
        ORDER BY p."ProductCode" ASC, pv."VariantName" ASC NULLS FIRST, sl."CreatedDate" ASC NULLS LAST
    """)).fetchall()

    # ── Colors — identical to Product Changes ────────────────────────────────
    BURGUNDY  = "6B0F2A"
    IVORY     = "F5F0E6"
    IVORY_ALT = "EDE8DC"
    WHITE     = "FFFFFF"
    BLACK     = "000000"
    GREEN_T   = "1A5C1A"
    RED_T     = "8B0000"
    BLUE_T    = "0A4F8A"

    thin = Side(style="thin",   color="C8BEB4")
    med  = Side(style="medium", color=BURGUNDY)

    def fill(h):      return PatternFill("solid", fgColor=h)
    def bord(l=None): return Border(left=l or thin, right=thin, top=thin, bottom=thin)
    C  = Alignment(horizontal="center", vertical="center")
    L  = Alignment(horizontal="left",   vertical="center", wrap_text=True)
    LC = Alignment(horizontal="left",   vertical="center")

    # ── Build hierarchy ───────────────────────────────────────────────────────
    products = OrderedDict()
    for r in rows:
        created_date, prod_code, prod_name, variant, size_band, size_cup, stock_id, prev_val, new_val, changed_by, role = r
        key = prod_code or "(no code)"
        if key not in products:
            products[key] = {"name": prod_name or "", "variants": OrderedDict()}
        vkey = variant or "(no variant)"
        if vkey not in products[key]["variants"]:
            products[key]["variants"][vkey] = []
        products[key]["variants"][vkey].append(r)

    # ── Workbook ──────────────────────────────────────────────────────────────
    wb = Workbook()
    ws = wb.active
    ws.title = "Stock Changes"

    NCOLS      = 10
    HDR_COLS   = ["Date",  "Time",  "Event",         "Size",  "Stock ID", "Previous", "New Total", "Change", "Changed By",  "Role"]
    COL_WIDTHS = [15,       9,       20,               12,      14,         12,          12,          10,       22,            16   ]

    ws.row_dimensions[1].height = 30
    for ci, (h, w) in enumerate(zip(HDR_COLS, COL_WIDTHS), 1):
        c = ws.cell(row=1, column=ci, value=h)
        c.font      = Font(name="Arial", bold=True, color=WHITE, size=10)
        c.fill      = fill(BURGUNDY)
        c.alignment = C
        c.border    = bord(med if ci == 1 else thin)
        ws.column_dimensions[get_column_letter(ci)].width = w
    ws.freeze_panes = "A2"

    row_idx       = 2
    total_records = len(rows)

    def write_merged(ri, text, bg, text_color, size=10, bold=True, height=22):
        ws.row_dimensions[ri].height = height
        for ci in range(1, NCOLS + 1):
            c = ws.cell(row=ri, column=ci, value="")
            c.fill   = fill(bg)
            c.border = bord(med if ci == 1 else thin)
        c0 = ws.cell(row=ri, column=1, value=text)
        c0.font      = Font(name="Arial", bold=bold, color=text_color, size=size)
        c0.fill      = fill(bg)
        c0.alignment = LC
        ws.merge_cells(start_row=ri, start_column=1, end_row=ri, end_column=NCOLS)

    def write_stock_row(ri, date_str, time_str, size, stock_id, prev, nxt, delta_val, by, role, row_bg):
        ws.row_dimensions[ri].height = 18
        delta_str = (f"+{delta_val}" if delta_val > 0 else str(delta_val)) if delta_val != 0 else "±0"
        delta_col = GREEN_T if delta_val > 0 else (RED_T if delta_val < 0 else BLACK)
        values = [date_str, time_str, "Total Stock Updated", size, stock_id, prev, nxt, delta_str, by, role]
        aligns = [C,         C,        C,                     C,    C,         C,    C,   C,          LC,  C  ]
        for ci, (val, aln) in enumerate(zip(values, aligns), 1):
            is_event = ci == 3
            is_delta = ci == 8
            c = ws.cell(row=ri, column=ci, value=val)
            c.font = Font(
                name="Arial",
                bold=(is_event or is_delta),
                color=(BLUE_T if is_event else delta_col if is_delta else BLACK),
                size=10
            )
            c.fill      = fill(row_bg)
            c.alignment = aln
            c.border    = bord(med if ci == 1 else thin)

    def write_spacer(ri, height=5):
        ws.row_dimensions[ri].height = height
        for ci in range(1, NCOLS + 1):
            ws.cell(row=ri, column=ci).fill   = fill(IVORY)
            ws.cell(row=ri, column=ci).border = bord(thin)

    for prod_code, pdata in products.items():
        prod_name = pdata["name"]

        # ══ Product header ════════════════════════════════════════════════════
        write_merged(row_idx, f"  {prod_code}   {prod_name}",
                     BURGUNDY, WHITE, size=11, height=26)
        row_idx += 1

        for vname, vrows in pdata["variants"].items():
            # Clean variant name
            vname_clean = vname
            if vname_clean.startswith(prod_name):
                vname_clean = vname_clean[len(prod_name):].lstrip(" -–—")
            if not vname_clean or vname_clean == "(no variant)":
                vname_clean = vname

            # ▸ Variant sub-header — identical style to product changes
            write_merged(row_idx, f"      {vname_clean}",
                         IVORY_ALT, BURGUNDY, size=10, height=20)
            row_idx += 1

            # Stock rows sorted by size then date
            vrows_sorted = sorted(vrows,
                key=lambda r: (str(r[4] or ""), str(r[0] or "")))
            for alt, r in enumerate(vrows_sorted):
                created_date, _, _, _, size_band, cup_size_label, stock_id, prev_val, new_val, changed_by, role = r
                date_str  = created_date.strftime("%d %b %Y") if created_date else ""
                time_str  = created_date.strftime("%H:%M")     if created_date else ""
                prev      = int(prev_val) if prev_val is not None else 0
                nxt       = int(new_val)  if new_val  is not None else 0
                delta_val = nxt - prev
                row_bg    = WHITE if alt % 2 == 0 else IVORY
                size_display = _fmt_size(size_band, cup_size_label) or ""
                write_stock_row(row_idx, date_str, time_str,
                    size_display, str(stock_id) if stock_id else "",
                    prev, nxt, delta_val, changed_by or "Admin", role or "Admin", row_bg)
                row_idx += 1

            write_spacer(row_idx, height=5)
            row_idx += 1

        write_spacer(row_idx, height=10)
        row_idx += 1

    # Summary — identical style
    ws.row_dimensions[row_idx].height = 20
    c = ws.cell(row=row_idx, column=1,
                value=f"  Total: {total_records} stock changes  ·  {len(products)} products")
    c.font      = Font(name="Arial", bold=True, size=10, color=BURGUNDY)
    c.fill      = fill(IVORY)
    c.alignment = LC
    ws.merge_cells(start_row=row_idx, start_column=1, end_row=row_idx, end_column=NCOLS)

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()