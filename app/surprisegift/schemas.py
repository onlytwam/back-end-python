from pydantic import BaseModel, field_validator
from typing import Optional, List
from datetime import datetime


class SurpriseGiftRuleCreate(BaseModel):
    giftName: str
    giftDescription: Optional[str] = None
    giftType: str  # 'physical_coupon' | 'physical_gift'
    minCartValue: float
    maxCartValue: Optional[float] = None
    sku: Optional[str] = None
    hsnCode: Optional[str] = None
    weight: Optional[float] = None
    isActive: Optional[bool] = True
    sortOrder: Optional[int] = 0

    @field_validator("giftType")
    @classmethod
    def validate_gift_type(cls, v: str) -> str:
        allowed = {"physical_coupon", "physical_gift"}
        if v not in allowed:
            raise ValueError(f"giftType must be one of: {allowed}")
        return v


class SurpriseGiftRuleUpdate(BaseModel):
    giftName: Optional[str] = None
    giftDescription: Optional[str] = None
    giftType: Optional[str] = None
    minCartValue: Optional[float] = None
    maxCartValue: Optional[float] = None
    sku: Optional[str] = None
    hsnCode: Optional[str] = None
    weight: Optional[float] = None
    isActive: Optional[bool] = None
    sortOrder: Optional[int] = None


class SurpriseGiftRuleOut(BaseModel):
    giftRuleId: int
    giftName: str
    giftDescription: Optional[str] = None
    giftType: str
    minCartValue: float
    maxCartValue: Optional[float] = None
    sku: Optional[str] = None
    hsnCode: Optional[str] = None
    weight: Optional[float] = None
    isActive: bool
    sortOrder: int
    createdDate: Optional[datetime] = None
    modifiedDate: Optional[datetime] = None

    class Config:
        from_attributes = True


# ── Public API (used by cart / order creation) ────────────────────────────────

class ActiveGiftTier(BaseModel):
    """Returned to the frontend for gift-tier display in cart."""
    giftRuleId: int
    threshold: float          # minCartValue — matches frontend naming
    label: str                # giftName
    displayPrice: float = 0   # always 0 (free for user)
    giftType: str             # so frontend knows coupon vs physical


class ActiveGiftTiersResponse(BaseModel):
    tiers: List[ActiveGiftTier]


# ── Gift lookup for ekart (internal) ─────────────────────────────────────────

class OrderGiftSnapshot(BaseModel):
    giftRuleId: int
    giftName: str
    giftDescription: Optional[str] = None
    giftType: str
    sku: Optional[str] = None
    hsnCode: Optional[str] = None
    weight: Optional[float] = None