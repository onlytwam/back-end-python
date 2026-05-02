from sqlalchemy import Column, BigInteger, Integer, String, Boolean, DateTime, Numeric, Text
from sqlalchemy.sql import func
from database import Base


class SurpriseGiftRule(Base):
    """
    Admin-defined rules mapping cart-value ranges to surprise gifts.

    giftType  : "physical_coupon" — paper coupon packed inside, no ekart item needed
              : "physical_gift"   — real product, must be added to ekart shipment
    """
    __tablename__ = "SurpriseGiftRule"
    __table_args__ = {"schema": "twam"}

    giftRuleId      = Column("GiftRuleId",      BigInteger,      primary_key=True, autoincrement=True)
    giftName        = Column("GiftName",         String(255),     nullable=False)
    giftDescription = Column("GiftDescription",  Text,            nullable=True)
    giftType        = Column("GiftType",         String(50),      nullable=False)  # 'physical_coupon' | 'physical_gift'
    minCartValue    = Column("MinCartValue",      Numeric(22, 2),  nullable=False)
    maxCartValue    = Column("MaxCartValue",      Numeric(22, 2),  nullable=True)   # NULL = no upper limit
    sku             = Column("SKU",              String(100),     nullable=True)
    hsnCode         = Column("HSNCode",          String(50),      nullable=True)
    weight          = Column("Weight",           Numeric(10, 3),  nullable=True)   # kg, for ekart
    isActive        = Column("IsActive",         Boolean,         default=True)
    sortOrder       = Column("SortOrder",        Integer,         default=0)
    createdDate     = Column("CreatedDate",      DateTime(timezone=True), server_default=func.now())
    modifiedDate    = Column("ModifiedDate",     DateTime(timezone=True), onupdate=func.now(), nullable=True)
    deletedInd      = Column("DeletedInd",       Boolean,         default=False)
    createdBy       = Column("CreatedBy",        String,          nullable=True)
    modifiedBy      = Column("ModifiedBy",       String,          nullable=True)
    organizationId  = Column("OrganizationId",   Integer,         nullable=True)


class OrderSurpriseGift(Base):
    """
    Links an order to the surprise-gift rule that was applied.
    Kept hidden from user until order state = 'Delivered'.
    """
    __tablename__ = "OrderSurpriseGift"
    __table_args__ = {"schema": "twam"}

    orderSurpriseGiftId = Column("OrderSurpriseGiftId", BigInteger, primary_key=True, autoincrement=True)
    orderId             = Column("OrderId",              BigInteger, nullable=False, index=True)
    giftRuleId          = Column("GiftRuleId",           BigInteger, nullable=False)
    # Snapshot fields so even if the rule is later edited, the original gift is preserved
    giftName            = Column("GiftName",             String(255), nullable=True)
    giftDescription     = Column("GiftDescription",      Text,        nullable=True)
    giftType            = Column("GiftType",             String(50),  nullable=True)
    sku                 = Column("SKU",                  String(100), nullable=True)
    hsnCode             = Column("HSNCode",              String(50),  nullable=True)
    weight              = Column("Weight",               Numeric(10, 3), nullable=True)
    isRevealedToUser    = Column("IsRevealedToUser",     Boolean,     default=False)
    createdDate         = Column("CreatedDate",          DateTime(timezone=True), server_default=func.now())
    modifiedDate        = Column("ModifiedDate",         DateTime(timezone=True), onupdate=func.now(), nullable=True)
    deletedInd          = Column("DeletedInd",           Boolean,     default=False)