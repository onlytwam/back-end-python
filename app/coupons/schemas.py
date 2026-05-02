from pydantic import BaseModel
from typing import Optional, List, Any
from datetime import datetime


# ─── Existing Digital Coupon Schemas ─────────────────────────────────────────

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
    couponType:      Optional[str]      = "generic"
    productId:       Optional[int]      = None


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
    couponType:      Optional[str]      = None
    productId:       Optional[int]      = None


class CouponResponse(BaseModel):
    couponId:        int
    couponCode:      Optional[str]      = None
    couponName:      Optional[str]      = None
    description:     Optional[str]      = None
    discount:        Optional[int]      = None
    startDate:       Optional[datetime] = None
    endDate:         Optional[datetime] = None
    state:           Optional[str]      = None
    isActive:        bool
    isCommon:        Optional[bool]     = None
    toUserProfileId: Optional[str]      = None
    couponType:      Optional[str]      = None
    productId:       Optional[int]      = None
    filters:         Optional[Any]      = None
    order:           Optional[Any]      = None
    page:            Optional[Any]      = None

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


class CouponMarkUsedRequest(BaseModel):
    couponId:      int
    userProfileId: str
    orderId:       Optional[int] = None


# ─── Physical Coupon Schemas ──────────────────────────────────────────────────

class PhysicalCouponValidateRequest(BaseModel):
    couponCode:    str
    userProfileId: Optional[str] = None


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


# ─── User Segmentation Schemas ────────────────────────────────────────────────

class SegmentDefinition(BaseModel):
    """Metadata about one segment type — returned by GET /UserSegments."""
    key:         str    # e.g. "most_frequent"
    label:       str    # e.g. "Most Frequent Buyers"
    description: str    # human-readable explanation
    icon:        str    # emoji icon for UI
    supportsBoth: bool  # whether "bottom N" makes sense for this segment


class SegmentListResponse(BaseModel):
    segments: List[SegmentDefinition]


class SegmentExcelRequest(BaseModel):
    """
    Body for POST /UserSegments/GenerateExcel
    Admin picks a segment + direction + count → gets an Excel of matching user IDs.
    """
    segmentKey:  str            # e.g. "most_frequent"
    direction:   str = "top"    # "top" | "bottom"
    count:       int = 50       # how many users to include


class SegmentPreviewRequest(BaseModel):
    """Body for POST /UserSegments/Preview — returns user list as JSON for UI preview."""
    segmentKey:  str
    direction:   str = "top"
    count:       int = 50


class SegmentUserRow(BaseModel):
    """One user row in a segment preview."""
    userId:    str
    name:      Optional[str] = None
    email:     Optional[str] = None
    phone:     Optional[str] = None
    metricLabel: Optional[str] = None   # e.g. "42 orders", "₹15,200 total"


class SegmentPreviewResponse(BaseModel):
    segmentKey:   str
    segmentLabel: str
    direction:    str
    count:        int
    users:        List[SegmentUserRow]


# ─── User-Specific Bulk Coupon Assignment Schemas ─────────────────────────────

class UserSpecificCouponUploadResponse(BaseModel):
    """Response after a successful Excel upload or segment-based bulk assign."""
    batchId:       int
    usersAssigned: int
    usersSkipped:  int
    message:       str


class UserCouponBatchResponse(BaseModel):
    """One row in the batch history list."""
    batchId:           int
    couponCode:        Optional[str]      = None
    couponName:        Optional[str]      = None
    discount:          Optional[int]      = None
    startDate:         Optional[datetime] = None
    endDate:           Optional[datetime] = None
    sourceType:        Optional[str]      = None   # "excel_upload" | "segment_generate"
    segmentKey:        Optional[str]      = None
    segmentDirection:  Optional[str]      = None
    userCount:         Optional[int]      = None
    createdDate:       Optional[datetime] = None
    createdBy:         Optional[str]      = None
    isActive:          bool               = True

    class Config:
        from_attributes = True


class UserCouponBatchListResponse(BaseModel):
    count: int
    list:  List[UserCouponBatchResponse]


class BulkAssignFromSegmentRequest(BaseModel):
    """
    Body for POST /UserSpecific/AssignFromSegment
    Admin selects a segment and fills in coupon details → assigns to all matched users.
    """
    segmentKey:  str
    direction:   str = "top"
    count:       int = 50
    couponCode:  str
    couponName:  str
    description: Optional[str] = None
    discount:    int
    startDate:   Optional[datetime] = None
    endDate:     Optional[datetime] = None