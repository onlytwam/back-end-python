from sqlalchemy import Column, BigInteger, String, Boolean, DateTime, Numeric, Integer
from sqlalchemy.sql import func
from database import Base


class ProductVariantDetail(Base):
    """Maps to twam.ProductVariantDetail (singular — NOT plural)"""
    __tablename__ = "ProductVariantDetail"
    __table_args__ = {"schema": "twam", "extend_existing": True}

    productVariantDetailId = Column("ProductVariantDetailId", BigInteger, primary_key=True, index=True, autoincrement=True)
    productId              = Column("ProductId",              BigInteger, nullable=False)
    productVariantId       = Column("ProductVariantId",       BigInteger, nullable=False)
    # productCode removed — ProductCode lives on twam.Products, not on the detail row
    # FK to mdm.Size.SizeId — stored as "Size" column (matches .NET entity)
    sizeId                 = Column("Size",                   BigInteger, nullable=True)
    stockQuantity          = Column("StockQuantity",          Integer,    nullable=True)
    processedQuantity      = Column("ProcessedQuantity",      Integer,    nullable=True)
    returnedQuantity       = Column("ReturnedQuantity",       Integer,    nullable=True)
    availableQuantity      = Column("AvailableQuantity",      Integer,    nullable=True)
    amendmentQuantity      = Column("AmendmentQuantity",      Integer,    nullable=True)
    discountPercent        = Column("DiscountPercent",        Integer,    nullable=True)
    mrpPrice               = Column("MRPPrice",               Numeric(22, 6), nullable=True)
    finalPrice             = Column("FinalPrice",             Numeric(22, 6), nullable=True)
    taxAmount              = Column("TaxAmount",              Numeric(22, 6), nullable=True)
    cgst                   = Column("CGST",                   Numeric(22, 6), nullable=True)
    sgst                   = Column("SGST",                   Numeric(22, 6), nullable=True)
    userProfileId          = Column("UserProfileId",          String,     nullable=True)
    state                  = Column("State",                  String,     nullable=True)
    stockId                = Column("StockId",                String,     nullable=True)
    # FK to mdm.CupSize.CupSizeId — stored as "CupSize" column
    cupSizeId              = Column("CupSize",                BigInteger, nullable=True)
    # StoreId / WarehouseId: DB has a CHECK constraint chk_store_xor_warehouse
    # that requires exactly one of these to be non-null. Both are exposed as
    # optional here; the repository supplies a default warehouseId when neither
    # is provided by the caller so the constraint is always satisfied.
    storeId                = Column("StoreId",                BigInteger, nullable=True)
    warehouseId            = Column("WarehouseId",            BigInteger, nullable=True)
    isLowStock             = Column("IsLowStock",             Boolean,    nullable=True)
    isOutOfStock           = Column("IsOutOfStock",           Boolean,    nullable=True)
    createdBy              = Column("CreatedBy",              String,     nullable=True)
    modifiedBy             = Column("ModifiedBy",             String,     nullable=True)
    createdDate            = Column("CreatedDate",            DateTime(timezone=True), server_default=func.now())
    modifiedDate           = Column("ModifiedDate",           DateTime(timezone=True), onupdate=func.now(), nullable=True)
    isActive               = Column("IsActive",               Boolean,    default=True,  server_default="true")
    deletedInd             = Column("DeletedInd",             Boolean,    default=False, server_default="false")