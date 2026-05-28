from datetime import datetime, timezone
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.shared.filters import apply_filters, apply_ordering, build_paged_response
from .models import TaxHSNCode


# ─────────────────────────── serialiser ──────────────────────────────────────

def _slab_dict(min_p, max_p, cgst, sgst, total, desc) -> Optional[dict]:
    """Return a slab dict only when the slab is actually populated."""
    if cgst is None and sgst is None:
        return None
    return {
        "minPrice":    float(min_p)  if min_p  is not None else 0,
        "maxPrice":    float(max_p)  if max_p  is not None else None,
        "cgst":        float(cgst)   if cgst   is not None else 0,
        "sgst":        float(sgst)   if sgst   is not None else 0,
        "totalGST":    float(total)  if total  is not None else round(float(cgst or 0) + float(sgst or 0), 6),
        "description": desc,
    }


def _to_dict(r: TaxHSNCode) -> dict:
    return {
        "taxHSNCodeId":   r.taxHSNCodeId,
        "hsnCode":        r.hsnCode,
        "hsnDescription": r.hsnDescription,
        # userProfileId and state do not exist in mdm."TaxHSNCode" — omitted
        "isActive":       r.isActive,
        "slab1": _slab_dict(
            r.slab1MinPrice, r.slab1MaxPrice,
            r.slab1CGST, r.slab1SGST, r.slab1TotalGST, r.slab1Description,
        ),
        "slab2": _slab_dict(
            r.slab2MinPrice, r.slab2MaxPrice,
            r.slab2CGST, r.slab2SGST, r.slab2TotalGST, r.slab2Description,
        ),
        "slab3": _slab_dict(
            r.slab3MinPrice, r.slab3MaxPrice,
            r.slab3CGST, r.slab3SGST, r.slab3TotalGST, r.slab3Description,
        ),
    }


# ─────────────────────────── helpers ─────────────────────────────────────────

def _apply_slab(entity: TaxHSNCode, slot: int, slab) -> None:
    """Write a SlabData (or None) into the numbered slab slot on the entity."""
    prefix = f"slab{slot}"
    if slab:
        setattr(entity, f"{prefix}MinPrice",    slab.minPrice)
        setattr(entity, f"{prefix}MaxPrice",    slab.maxPrice)
        setattr(entity, f"{prefix}CGST",        slab.cgst)
        setattr(entity, f"{prefix}SGST",        slab.sgst)
        setattr(entity, f"{prefix}TotalGST",    slab.totalGST or round(slab.cgst + slab.sgst, 6))
        setattr(entity, f"{prefix}Description", slab.description)
    else:
        for attr in ("MinPrice", "MaxPrice", "CGST", "SGST", "TotalGST", "Description"):
            setattr(entity, f"{prefix}{attr}", None)


# ─────────────────────────── CRUD ────────────────────────────────────────────

def get_all(db, filters, order_ascending, order_property, page_index, page_size):
    query = db.query(TaxHSNCode).filter(
        TaxHSNCode.deletedInd == False,
        TaxHSNCode.isActive == True,
    )
    query = apply_filters(query, TaxHSNCode, filters)
    query = apply_ordering(query, TaxHSNCode, order_property, order_ascending)
    total = query.count()
    if page_index and page_size:
        query = query.offset((page_index - 1) * page_size).limit(page_size)
    return build_paged_response(total, [_to_dict(r) for r in query.all()])


def get_by_id(db, tax_id: int):
    entity = db.query(TaxHSNCode).filter(
        TaxHSNCode.taxHSNCodeId == tax_id,
        TaxHSNCode.deletedInd == False,
    ).first()
    return _to_dict(entity) if entity else None


def create(db: Session, data) -> int:
    entity = TaxHSNCode(
        hsnCode        = data.hsnCode,
        hsnDescription = data.hsnDescription,
        # userProfileId and state are NOT columns in the DB — excluded
        isActive       = data.isActive,
        createdDate    = datetime.now(timezone.utc),
        createdBy      = getattr(data, "createdBy", None) or "system",
        deletedInd     = False,
    )
    _apply_slab(entity, 1, data.slab1)
    _apply_slab(entity, 2, getattr(data, "slab2", None))
    _apply_slab(entity, 3, getattr(data, "slab3", None))
    db.add(entity)
    db.commit()
    db.refresh(entity)
    return entity.taxHSNCodeId


def update(db: Session, data) -> Optional[int]:
    entity = db.query(TaxHSNCode).filter(
        TaxHSNCode.taxHSNCodeId == data.taxHSNCodeId,
        TaxHSNCode.deletedInd == False,
    ).first()
    if not entity:
        return None
    entity.hsnCode        = data.hsnCode
    entity.hsnDescription = data.hsnDescription
    # userProfileId and state are NOT columns in the DB — excluded
    entity.isActive       = data.isActive
    entity.modifiedDate   = datetime.now(timezone.utc)
    entity.modifiedBy     = getattr(data, "modifiedBy", None)
    _apply_slab(entity, 1, data.slab1)
    _apply_slab(entity, 2, getattr(data, "slab2", None))
    _apply_slab(entity, 3, getattr(data, "slab3", None))
    db.commit()

    # ── Cascade: recalculate CGST/SGST on all PVDs that use this HSN code ──
    # Runs silently — the HSN save is committed; PVD errors are non-fatal.
    try:
        recalculate_pvds_for_hsn(db, entity.taxHSNCodeId)
    except Exception:
        pass

    return entity.taxHSNCodeId


def delete(db: Session, tax_id: int) -> bool:
    entity = db.query(TaxHSNCode).filter(TaxHSNCode.taxHSNCodeId == tax_id).first()
    if not entity:
        return False
    entity.deletedInd   = True
    entity.modifiedDate = datetime.now(timezone.utc)
    db.commit()
    return True


# ─────────────────────────── Core tax computation ────────────────────────────

def compute_tax_for_product(db: Session, product_id: int, final_price: float) -> dict:
    """
    Given a product and its finalPrice, return CGST/SGST amounts.

    Logic:
      1. Look up the product's TaxHSNCodeId from twam.Products.
      2. Load the TaxHSNCode row (single table, slabs embedded).
      3. Walk Slab1 → Slab2 → Slab3 in MinPrice ASC order; use the first
         slab whose range covers final_price.
         Range match: minPrice <= price AND (maxPrice IS NULL OR price <= maxPrice)
      4. Amounts = rate% × finalPrice (tax is included in MRP).
    """
    if not product_id or final_price is None:
        return _zero_tax()

    # Step 1: get TaxHSNCodeId from the product
    row = db.execute(
        text('SELECT "TaxHSNCodeId" FROM twam."Products" WHERE "ProductId" = :pid AND "DeletedInd" = false'),
        {"pid": product_id},
    ).fetchone()

    if not row or not row[0]:
        return _zero_tax()

    hsn_id = int(row[0])

    # Step 2: load the TaxHSNCode row
    entity = db.query(TaxHSNCode).filter(
        TaxHSNCode.taxHSNCodeId == hsn_id,
        TaxHSNCode.isActive   == True,
        TaxHSNCode.deletedInd == False,
    ).first()

    if not entity:
        return _zero_tax()

    # Step 3: find the matching slab (walk Slab1 → Slab2 → Slab3)
    slabs = [
        (entity.slab1MinPrice, entity.slab1MaxPrice, entity.slab1CGST, entity.slab1SGST, entity.slab1TotalGST, entity.slab1Description),
        (entity.slab2MinPrice, entity.slab2MaxPrice, entity.slab2CGST, entity.slab2SGST, entity.slab2TotalGST, entity.slab2Description),
        (entity.slab3MinPrice, entity.slab3MaxPrice, entity.slab3CGST, entity.slab3SGST, entity.slab3TotalGST, entity.slab3Description),
    ]

    matched_cgst = matched_sgst = matched_total = None
    matched_desc = None

    for (min_p, max_p, cgst, sgst, total, desc) in slabs:
        if cgst is None:          # slab not populated
            continue
        min_val = float(min_p) if min_p is not None else 0
        if final_price < min_val:
            continue
        if max_p is not None and final_price > float(max_p):
            continue
        # match found
        matched_cgst  = float(cgst)
        matched_sgst  = float(sgst)
        matched_total = float(total) if total else round(matched_cgst + matched_sgst, 6)
        matched_desc  = desc or f"Slab (≤ ₹{max_p})" if max_p else "Slab (no upper limit)"
        break

    if matched_cgst is None:
        # price didn't match any slab — fall back to the last populated slab
        for (min_p, max_p, cgst, sgst, total, desc) in reversed(slabs):
            if cgst is not None:
                matched_cgst  = float(cgst)
                matched_sgst  = float(sgst)
                matched_total = float(total) if total else round(matched_cgst + matched_sgst, 6)
                matched_desc  = (desc or "Fallback slab")
                break

    if matched_cgst is None:
        return _zero_tax()

    # Step 4: calculate amounts
    cgst_amount = round(final_price * matched_cgst / 100, 2)
    sgst_amount = round(final_price * matched_sgst / 100, 2)

    return {
        "taxHSNCodeId": hsn_id,
        "hsnCode":      entity.hsnCode,
        "cgstRate":     matched_cgst,
        "sgstRate":     matched_sgst,
        "totalGSTRate": matched_total,
        "cgstAmount":   cgst_amount,
        "sgstAmount":   sgst_amount,
        "taxAmount":    round(cgst_amount + sgst_amount, 2),
        "slabApplied":  matched_desc,
    }


def _zero_tax() -> dict:
    return {
        "taxHSNCodeId": None,
        "hsnCode":      None,
        "cgstRate":     0.0,
        "sgstRate":     0.0,
        "totalGSTRate": 0.0,
        "cgstAmount":   0.0,
        "sgstAmount":   0.0,
        "taxAmount":    0.0,
        "slabApplied":  None,
    }

# ─────────────────────────── Cascade recalculation ───────────────────────────

def recalculate_pvds_for_hsn(db, hsn_id: int) -> dict:
    """
    When a TaxHSNCode's slab rates are edited, re-compute CGST/SGST/TaxAmount
    for every PVD that belongs to any product linked to this HSN code.

    Called automatically after `update()`.  Also exposed via a dedicated
    admin endpoint so operators can trigger a manual sweep (e.g. after a
    GST rate change).

    Returns {"productsAffected": P, "pvdsUpdated": N, "pvdsSkipped": M,
             "errors": [...]}.
    """
    from app.products.repository import recalculate_pvd_taxes_for_product

    # Find all products that reference this HSN code
    try:
        rows = db.execute(
            text('SELECT "ProductId" FROM twam."Products" '                 'WHERE "TaxHSNCodeId" = :hid AND "DeletedInd" = false'),
            {"hid": hsn_id},
        ).fetchall()
    except Exception as exc:
        return {"productsAffected": 0, "pvdsUpdated": 0, "pvdsSkipped": 0,
                "errors": [str(exc)]}

    product_ids = [r[0] for r in rows]
    total_updated = total_skipped = 0
    all_errors: list = []

    for pid in product_ids:
        result = recalculate_pvd_taxes_for_product(db, pid)
        total_updated += result.get("updated", 0)
        total_skipped += result.get("skipped", 0)
        all_errors.extend(result.get("errors", []))

    return {
        "productsAffected": len(product_ids),
        "pvdsUpdated":      total_updated,
        "pvdsSkipped":      total_skipped,
        "errors":           all_errors,
    }