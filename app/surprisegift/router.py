"""
Surprise Gift API
─────────────────────────────────────────────────────────────────────────────
PUBLIC (cart / order creation)
  GET  /api/SurpriseGift/Tiers            — fetch active tiers for cart UI
  GET  /api/SurpriseGift/Resolve          — resolve gift for a cart value

ADMIN
  GET    /api/SurpriseGift/Admin/Rules         — list all rules (incl. inactive)
  POST   /api/SurpriseGift/Admin/Rules         — create rule
  PUT    /api/SurpriseGift/Admin/Rules/{id}    — update rule
  DELETE /api/SurpriseGift/Admin/Rules/{id}    — soft-delete rule

  GET    /api/SurpriseGift/Admin/Order/{order_id} — get gift linked to an order
"""

from __future__ import annotations

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query

from sqlalchemy.orm import Session
from database import get_db

from .schemas import (
    SurpriseGiftRuleCreate,
    SurpriseGiftRuleUpdate,
    SurpriseGiftRuleOut,
    ActiveGiftTier,
    ActiveGiftTiersResponse,
    OrderGiftSnapshot,
)
from . import repository

router = APIRouter(
    prefix="/api/SurpriseGift",
    tags=["Surprise Gift"],
)


# ── PUBLIC endpoints ──────────────────────────────────────────────────────────

@router.get("/Tiers", response_model=ActiveGiftTiersResponse)
def get_active_tiers(db: Session = Depends(get_db)):
    """
    Return all active gift tiers sorted by threshold descending.
    Used by the cart page to decide which gift banner to show.
    """
    rules = repository.list_rules(db, include_inactive=False)
    tiers = [
        ActiveGiftTier(
            giftRuleId=r.giftRuleId,
            threshold=float(r.minCartValue),
            label=r.giftName,
            displayPrice=0.0,
            giftType=r.giftType,
        )
        for r in rules
    ]
    return ActiveGiftTiersResponse(tiers=tiers)


@router.get("/Resolve", response_model=Optional[ActiveGiftTier])
def resolve_gift_for_cart(
    cart_value: float = Query(..., gt=0, description="Current cart subtotal"),
    db: Session = Depends(get_db),
):
    """Resolve the single best gift for the given cart value."""
    rule = repository.get_applicable_gift_rule(db, cart_value)
    if not rule:
        return None
    return ActiveGiftTier(
        giftRuleId=rule.giftRuleId,
        threshold=float(rule.minCartValue),
        label=rule.giftName,
        displayPrice=0.0,
        giftType=rule.giftType,
    )


# ── ADMIN endpoints ───────────────────────────────────────────────────────────

@router.get("/Admin/Rules", response_model=List[SurpriseGiftRuleOut])
def list_rules(
    include_inactive: bool = Query(False),
    db: Session = Depends(get_db),
):
    return repository.list_rules(db, include_inactive=include_inactive)


@router.post("/Admin/Rules", response_model=SurpriseGiftRuleOut, status_code=201)
def create_rule(
    data: SurpriseGiftRuleCreate,
    db: Session = Depends(get_db),
):
    return repository.create_rule(db, data)


@router.put("/Admin/Rules/{gift_rule_id}", response_model=SurpriseGiftRuleOut)
def update_rule(
    gift_rule_id: int,
    data: SurpriseGiftRuleUpdate,
    db: Session = Depends(get_db),
):
    rule = repository.update_rule(db, gift_rule_id, data)
    if not rule:
        raise HTTPException(404, f"Gift rule {gift_rule_id} not found")
    return rule


@router.delete("/Admin/Rules/{gift_rule_id}")
def delete_rule(
    gift_rule_id: int,
    db: Session = Depends(get_db),
):
    ok = repository.delete_rule(db, gift_rule_id)
    if not ok:
        raise HTTPException(404, f"Gift rule {gift_rule_id} not found")
    return {"success": True}


@router.get("/Admin/Order/{order_id}", response_model=Optional[OrderGiftSnapshot])
def get_order_gift(
    order_id: int,
    db: Session = Depends(get_db),
):
    """Return the gift snapshot for an order (admin view — all details visible)."""
    return repository.get_order_gift_snapshot(db, order_id)