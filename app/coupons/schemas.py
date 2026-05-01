from pydantic import BaseModel
from typing import Optional, List, Any
from datetime import datetime


# ─── Digital Coupon Schemas ───────────────────────────────────────────────────

class CouponCreate(BaseModel):
    couponCode:      Optional[str]      = None
    couponName:      Optional[str]      = None
    description:     Optional[str]      = None
    discount:        Optional[int]      = None
    startDate:       Optional[datetime] = None
    endDate:         Optional[datetime] = None
    state:           Optional[str]      = None
    isActive:        bool               = True
    isCommon:        Optional[bool]     = None
    toUserProfileId: Optional[str]      = None
    userProfileId:   Optional[str]      = None
    # NEW
    couponType:  Optional[str] = "generic"   # "generic" | "user_specific" | "product_specific"
    productId:   Optional[int] = None        # only for product_specific


class CouponUpdate(BaseModel):
    couponCode:      Optional[str]      = None
    couponName:      Optional[str]      = None
    description:     Optional[str]      = None
    discount:        Optional[int]      = None
    startDate:       Optional[datetime] = None
    endDate:         Optional[datetime] = None
    state:           Optional[str]      = None
    isActive:        Optional[bool]     = None
    isCommon:        Optional[bool]     = None
    toUserProfileId: Optional[str]      = None
    userProfileId:   Optional[str]      = None
    # NEW
    couponType:  Optional[str] = None
    productId:   Optional[int] = None


class CouponResponse(BaseModel):
    couponId:        int
    couponCode:      Optional[str]  = None
    couponName:      Optional[str]  = None
    description:     Optional[str]  = None
    discount:        Optional[int]  = None
    startDate:       Optional[datetime] = None
    endDate:         Optional[datetime] = None
    state:           Optional[str]  = None
    isActive:        bool
    isCommon:        Optional[bool] = None
    toUserProfileId: Optional[str]  = None
    couponType:      Optional[str]  = None
    productId:       Optional[int]  = None
    filters:         Optional[Any]  = None
    order:           Optional[Any]  = None
    page:            Optional[Any]  = None

    class Config:
        from_attributes = True


class CouponUserResponse(CouponResponse):
    isExpire: Optional[bool] = None


class CouponListResponse(BaseModel):
    count:      int
    list:       List[CouponResponse]
    parameters: Optional[Any] = None


class CouponUserListResponse(BaseModel):
    count:      int
    list:       List[CouponUserResponse]
    parameters: Optional[Any] = None


# ─── Mark-Used Schemas ────────────────────────────────────────────────────────

class CouponMarkUsedRequest(BaseModel):
    couponId:      int
    userProfileId: str
    orderId:       Optional[int] = None


# ─── Physical Coupon Schemas ──────────────────────────────────────────────────

class PhysicalCouponValidateRequest(BaseModel):
    couponCode:    str
    userProfileId: Optional[str] = None   # for audit; not used for uniqueness


class PhysicalCouponMarkUsedRequest(BaseModel):
    couponCode:    str
    userProfileId: str
    orderId:       Optional[int] = None


class PhysicalCouponResponse(BaseModel):
    physicalCouponId:    int
    couponCode:          str
    discount:            Optional[int]      = None
    description:         Optional[str]      = None
    isUsed:              bool
    usedByUserProfileId: Optional[str]      = None
    usedAt:              Optional[datetime] = None
    orderId:             Optional[int]      = None
    createdDate:         Optional[datetime] = None

    class Config:
        from_attributes = True


class PhysicalCouponListResponse(BaseModel):
    count: int
    list:  List[PhysicalCouponResponse]


class PhysicalCouponValidateResponse(BaseModel):
    valid:       bool
    couponCode:  str
    discount:    Optional[int] = None
    description: Optional[str] = None
    message:     Optional[str] = None