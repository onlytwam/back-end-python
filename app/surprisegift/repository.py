from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import text

from .models import SurpriseGiftRule, OrderSurpriseGift
from .schemas import SurpriseGiftRuleCreate, SurpriseGiftRuleUpdate, OrderGiftSnapshot


# ── Admin CRUD ────────────────────────────────────────────────────────────────

def list_rules(db: Session, include_inactive: bool = False):
    q = db.query(SurpriseGiftRule).filter(SurpriseGiftRule.deletedInd == False)
    if not include_inactive:
        q = q.filter(SurpriseGiftRule.isActive == True)
    return q.order_by(SurpriseGiftRule.sortOrder, SurpriseGiftRule.minCartValue.desc()).all()


def get_rule(db: Session, gift_rule_id: int) -> Optional[SurpriseGiftRule]:
    return (
        db.query(SurpriseGiftRule)
        .filter(SurpriseGiftRule.giftRuleId == gift_rule_id, SurpriseGiftRule.deletedInd == False)
        .first()
    )


def create_rule(db: Session, data: SurpriseGiftRuleCreate, created_by: str = "") -> SurpriseGiftRule:
    rule = SurpriseGiftRule(
        giftName=data.giftName,
        giftDescription=data.giftDescription,
        giftType=data.giftType,
        minCartValue=data.minCartValue,
        maxCartValue=data.maxCartValue,
        sku=data.sku,
        hsnCode=data.hsnCode,
        weight=data.weight,
        isActive=data.isActive if data.isActive is not None else True,
        sortOrder=data.sortOrder or 0,
        createdBy=created_by,
        deletedInd=False,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


def update_rule(db: Session, gift_rule_id: int, data: SurpriseGiftRuleUpdate, modified_by: str = "") -> Optional[SurpriseGiftRule]:
    rule = get_rule(db, gift_rule_id)
    if not rule:
        return None

    for field, value in data.model_dump(exclude_none=True).items():
        setattr(rule, field, value)

    rule.modifiedDate = datetime.now(timezone.utc)
    rule.modifiedBy = modified_by
    db.commit()
    db.refresh(rule)
    return rule


def delete_rule(db: Session, gift_rule_id: int) -> bool:
    rule = get_rule(db, gift_rule_id)
    if not rule:
        return False
    rule.deletedInd = True
    rule.modifiedDate = datetime.now(timezone.utc)
    db.commit()
    return True


# ── Gift Tier resolution (for frontend cart) ──────────────────────────────────

def get_applicable_gift_rule(db: Session, cart_value: float) -> Optional[SurpriseGiftRule]:
    """
    Find the best (highest-threshold) gift rule that applies for a given cart value.
    Rules with maxCartValue=NULL treat that as 'no upper limit'.
    """
    rows = (
        db.query(SurpriseGiftRule)
        .filter(
            SurpriseGiftRule.deletedInd == False,
            SurpriseGiftRule.isActive == True,
            SurpriseGiftRule.minCartValue <= cart_value,
        )
        .order_by(SurpriseGiftRule.minCartValue.desc())
        .all()
    )
    for rule in rows:
        if rule.maxCartValue is None or float(rule.maxCartValue) >= cart_value:
            return rule
    return None


# ── Order surprise-gift linkage ───────────────────────────────────────────────

def attach_gift_to_order(db: Session, order_id: int, rule: SurpriseGiftRule) -> OrderSurpriseGift:
    """
    Snapshot the gift rule into OrderSurpriseGift so it's preserved
    even if the admin later edits the rule.
    """
    link = OrderSurpriseGift(
        orderId=order_id,
        giftRuleId=rule.giftRuleId,
        giftName=rule.giftName,
        giftDescription=rule.giftDescription,
        giftType=rule.giftType,
        sku=rule.sku,
        hsnCode=rule.hsnCode,
        weight=rule.weight,
        isRevealedToUser=False,
        deletedInd=False,
    )
    db.add(link)
    db.flush()
    return link


def get_order_gift(db: Session, order_id: int) -> Optional[OrderSurpriseGift]:
    return (
        db.query(OrderSurpriseGift)
        .filter(OrderSurpriseGift.orderId == order_id, OrderSurpriseGift.deletedInd == False)
        .first()
    )


def get_order_gift_snapshot(db: Session, order_id: int) -> Optional[OrderGiftSnapshot]:
    """Return gift details for ekart shipment packaging."""
    gift = get_order_gift(db, order_id)
    if not gift:
        return None
    return OrderGiftSnapshot(
        giftRuleId=gift.giftRuleId,
        giftName=gift.giftName or "",
        giftDescription=gift.giftDescription,
        giftType=gift.giftType or "physical_coupon",
        sku=gift.sku,
        hsnCode=gift.hsnCode,
        weight=float(gift.weight) if gift.weight else None,
    )


def reveal_gift_to_user(db: Session, order_id: int) -> bool:
    """Called when order state transitions to 'Delivered'."""
    gift = get_order_gift(db, order_id)
    if not gift:
        return False
    gift.isRevealedToUser = True
    gift.modifiedDate = datetime.now(timezone.utc)
    db.commit()
    return True