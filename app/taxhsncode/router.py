import json
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from database import get_db
from . import schemas, repository
from app.auth.dependencies import get_current_user

router = APIRouter(
    dependencies=[Depends(get_current_user)],
    prefix="/api/TaxHSNCode",
    tags=["TaxHSNCode"],
)


def _parse_filters(raw: Optional[str]) -> Optional[list]:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else [parsed]
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Filters format.")


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("", response_model=schemas.TaxHSNCodeListResponse)
def get_list(
    Filters:         Optional[str]  = Query(None, alias="Filters"),
    Order_Ascending: Optional[bool] = Query(None, alias="Order.Ascending"),
    Order_Property:  Optional[str]  = Query(None, alias="Order.Property"),
    Page_Index:      Optional[int]  = Query(None, alias="Page.Index", ge=1),
    Page_Size:       Optional[int]  = Query(None, alias="Page.Size",  ge=1),
    db: Session = Depends(get_db),
):
    return repository.get_all(
        db, _parse_filters(Filters), Order_Ascending, Order_Property, Page_Index, Page_Size,
    )


# ── Detail ────────────────────────────────────────────────────────────────────

@router.get("/{tax_id}", response_model=schemas.TaxHSNCodeResponse)
def get_detail(tax_id: int, db: Session = Depends(get_db)):
    result = repository.get_by_id(db, tax_id)
    if not result:
        raise HTTPException(status_code=404, detail="TaxHSNCode not found.")
    return result


# ── Create ────────────────────────────────────────────────────────────────────

@router.post("", status_code=201)
def create(command: schemas.TaxHSNCodeCreate, db: Session = Depends(get_db)):
    return {"taxHSNCodeId": repository.create(db, command)}


# ── Update ────────────────────────────────────────────────────────────────────

@router.put("/{tax_id}")
def update(tax_id: int, command: schemas.TaxHSNCodeUpdate, db: Session = Depends(get_db)):
    command.taxHSNCodeId = tax_id
    result = repository.update(db, command)
    if not result:
        raise HTTPException(status_code=404, detail="TaxHSNCode not found.")
    return {"taxHSNCodeId": result}


# ── Delete ────────────────────────────────────────────────────────────────────

@router.delete("/{tax_id}", status_code=204)
def delete(tax_id: int, db: Session = Depends(get_db)):
    if not repository.delete(db, tax_id):
        raise HTTPException(status_code=404, detail="TaxHSNCode not found.")


# ── Utility: compute tax for a product + price ───────────────────────────────

@router.get("/Compute/{product_id}")
def compute_tax(
    product_id: int,
    price: float = Query(..., description="finalPrice of the ProductVariantDetail"),
    db: Session = Depends(get_db),
):
    """
    Returns CGST / SGST amounts for the given product and finalPrice.
    Walks the product's TaxHSNCode slabs to find the correct rate bracket.
    The admin UI calls this when MRP / finalPrice changes so fields can be
    auto-populated before saving.
    """
    return repository.compute_tax_for_product(db, product_id, price)

# ── ADMIN: recalculate all PVDs that use a specific HSN code ─────────────────
@router.post(
    "/{tax_id}/recalculate-pvds",
    summary="Recalculate CGST/SGST on all PVDs linked to this HSN code",
)
def recalculate_pvds(tax_id: int, db: Session = Depends(get_db)):
    """
    Finds every product that has this TaxHSNCode attached and re-computes
    CGST, SGST, and TaxAmount on all their PVDs using the current slab rates.

    Called automatically when slab rates are updated via PUT /{tax_id}.
    Also available as a manual trigger for admin reconciliation.
    """
    result = repository.recalculate_pvds_for_hsn(db, tax_id)
    return {"taxHSNCodeId": tax_id, **result}