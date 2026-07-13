from sqlalchemy import Column, BigInteger, Integer, String, Boolean, DateTime, Text
from database import Base


class Coupons(Base):
    __tablename__ = "Coupons"
    __table_args__ = {"schema": "coupons"}

    couponId        = Column("CouponId",        BigInteger, primary_key=True, index=True)
    couponCode      = Column("CouponCode",      String,  nullable=True)
    couponName      = Column("CouponName",      String,  nullable=True)
    description     = Column("Description",     String,  nullable=True)
    discount        = Column("Discount",        Integer, nullable=True)
    startDate       = Column("StartDate",       DateTime(timezone=True), nullable=True)
    endDate         = Column("EndDate",         DateTime(timezone=True), nullable=True)
    state           = Column("State",           String,  nullable=True)
    isActive        = Column("IsActive",        Boolean, default=True)
    isCommon        = Column("IsCommon",        Boolean, nullable=True)
    toUserProfileId = Column("ToUserProfileId", String,  nullable=True)
    userProfileId   = Column("UserProfileId",   String,  nullable=True)
    createdDate     = Column("CreatedDate",     DateTime(timezone=True), nullable=True)
    modifiedDate    = Column("ModifiedDate",    DateTime(timezone=True), nullable=True)
    deletedInd      = Column("DeletedInd",      Boolean, default=False)
    createdBy       = Column("CreatedBy",       String,  nullable=True)
    modifiedBy      = Column("ModifiedBy",      String,  nullable=True)
    organizationId  = Column("OrganizationId",  Integer, nullable=True)
    couponType      = Column("CouponType",      String,  nullable=True, default="generic")
    productId       = Column("ProductId",       BigInteger, nullable=True)

    # NEW: links this row to a bulk-assignment batch
    batchId         = Column("BatchId",         BigInteger, nullable=True, index=True)

    # BRAND COUPON fields
    # brandId: links coupon to a brand — only applicable to products of this brand
    brandId         = Column("BrandId",         Integer,   nullable=True, index=True)
    # isSingleUse: once ANY user uses this code, it becomes invalid for all others
    isSingleUse     = Column("IsSingleUse",     Boolean,   nullable=True, default=False)
    # isUsed: set to True the first time this single-use coupon is redeemed
    isUsed          = Column("IsUsed",          Boolean,   nullable=True, default=False)


class CouponUsage(Base):
    __tablename__ = "CouponUsage"
    __table_args__ = {"schema": "coupons"}

    usageId       = Column("UsageId",       BigInteger, primary_key=True, index=True, autoincrement=True)
    couponId      = Column("CouponId",      BigInteger, nullable=False, index=True)
    userProfileId = Column("UserProfileId", String,     nullable=False, index=True)
    usedAt        = Column("UsedAt",        DateTime(timezone=True), nullable=True)
    orderId       = Column("OrderId",       BigInteger, nullable=True)


class PhysicalCoupon(Base):
    __tablename__ = "PhysicalCoupons"
    __table_args__ = {"schema": "coupons"}

    physicalCouponId    = Column("PhysicalCouponId",    BigInteger, primary_key=True, index=True, autoincrement=True)
    couponCode          = Column("CouponCode",          String,  nullable=False, unique=True, index=True)
    discount            = Column("Discount",            Integer, nullable=True)
    description         = Column("Description",         String,  nullable=True)
    isUsed              = Column("IsUsed",              Boolean, default=False)
    usedByUserProfileId = Column("UsedByUserProfileId", String,  nullable=True)
    usedAt              = Column("UsedAt",              DateTime(timezone=True), nullable=True)
    orderId             = Column("OrderId",             BigInteger, nullable=True)
    createdDate         = Column("CreatedDate",         DateTime(timezone=True), nullable=True)
    organizationId      = Column("OrganizationId",      Integer, nullable=True)


class UserCouponBatch(Base):
    """
    Tracks every bulk user-specific coupon assignment.
    One row per admin upload / segment-generate action.
    Lets admin see history of who was targeted and with what coupon settings.
    """
    __tablename__ = "UserCouponBatch"
    __table_args__ = {"schema": "coupons"}

    batchId        = Column("BatchId",        BigInteger, primary_key=True, index=True, autoincrement=True)
    # Coupon details applied to everyone in this batch
    couponCode     = Column("CouponCode",     String,  nullable=True)
    couponName     = Column("CouponName",     String,  nullable=True)
    description    = Column("Description",    String,  nullable=True)
    discount       = Column("Discount",       Integer, nullable=True)
    startDate      = Column("StartDate",      DateTime(timezone=True), nullable=True)
    endDate        = Column("EndDate",        DateTime(timezone=True), nullable=True)
    # How this batch was created: "excel_upload" | "segment_generate"
    sourceType     = Column("SourceType",     String,  nullable=True)
    # If segment: which segment key was used (e.g. "most_frequent")
    segmentKey     = Column("SegmentKey",     String,  nullable=True)
    # If segment: "top" or "bottom"
    segmentDirection = Column("SegmentDirection", String, nullable=True)
    # How many users were targeted
    userCount      = Column("UserCount",      Integer, nullable=True)
    # JSON snapshot of user IDs for audit (stored as Text)
    userIdsSnapshot = Column("UserIdsSnapshot", Text, nullable=True)
    organizationId = Column("OrganizationId", Integer, nullable=True)
    createdDate    = Column("CreatedDate",    DateTime(timezone=True), nullable=True)
    createdBy      = Column("CreatedBy",      String,  nullable=True)
    isActive       = Column("IsActive",       Boolean, default=True)