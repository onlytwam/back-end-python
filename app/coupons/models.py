from sqlalchemy import Column, BigInteger, Integer, String, Boolean, DateTime
from database import Base


class Coupons(Base):
    __tablename__ = "Coupons"
    __table_args__ = {"schema": "coupons"}

    couponId       = Column("CouponId",       BigInteger, primary_key=True, index=True)
    couponCode     = Column("CouponCode",     String,  nullable=True)
    couponName     = Column("CouponName",     String,  nullable=True)
    description    = Column("Description",    String,  nullable=True)
    discount       = Column("Discount",       Integer, nullable=True)
    startDate      = Column("StartDate",      DateTime(timezone=True), nullable=True)
    endDate        = Column("EndDate",        DateTime(timezone=True), nullable=True)
    state          = Column("State",          String,  nullable=True)
    isActive       = Column("IsActive",       Boolean, default=True)
    isCommon       = Column("IsCommon",       Boolean, nullable=True)
    toUserProfileId= Column("ToUserProfileId",String,  nullable=True)
    userProfileId  = Column("UserProfileId",  String,  nullable=True)
    createdDate    = Column("CreatedDate",    DateTime(timezone=True), nullable=True)
    modifiedDate   = Column("ModifiedDate",   DateTime(timezone=True), nullable=True)
    deletedInd     = Column("DeletedInd",     Boolean, default=False)
    createdBy      = Column("CreatedBy",      String,  nullable=True)
    modifiedBy     = Column("ModifiedBy",     String,  nullable=True)
    organizationId = Column("OrganizationId", Integer, nullable=True)

    # NEW: "generic" | "user_specific" | "product_specific"
    couponType = Column("CouponType", String, nullable=True, default="generic")
    # NEW: used only when couponType == "product_specific"
    productId  = Column("ProductId",  BigInteger, nullable=True)


class CouponUsage(Base):
    """
    Records that a digital coupon has been redeemed by a specific user.
    Once a row exists for (couponId, userProfileId) the coupon is hidden
    everywhere for that user and cannot be applied again.
    """
    __tablename__ = "CouponUsage"
    __table_args__ = {"schema": "coupons"}

    usageId       = Column("UsageId",       BigInteger, primary_key=True, index=True, autoincrement=True)
    couponId      = Column("CouponId",      BigInteger, nullable=False, index=True)
    userProfileId = Column("UserProfileId", String,     nullable=False, index=True)
    usedAt        = Column("UsedAt",        DateTime(timezone=True), nullable=True)
    orderId       = Column("OrderId",       BigInteger, nullable=True)


class PhysicalCoupon(Base):
    """
    Store-issued physical coupon codes loaded by admin via Excel upload.
    Single-use globally: once any user redeems it the code is permanently dead.
    Never shown in user profile or cart dropdown — pure server-side validation.
    """
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