from sqlalchemy import Column, BigInteger, Integer, String, DateTime, Boolean
from sqlalchemy.sql import func
from database import Base


class StockLog(Base):
    """Audit log for every stock change on a ProductVariantDetail row."""
    __tablename__ = "StockLog"
    __table_args__ = {"schema": "twam", "extend_existing": True}

    stockLogId             = Column("StockLogId",             BigInteger, primary_key=True, index=True, autoincrement=True)
    productVariantDetailId = Column("ProductVariantDetailId", BigInteger, nullable=False, index=True)
    productId              = Column("ProductId",              BigInteger, nullable=True)
    productVariantId       = Column("ProductVariantId",       BigInteger, nullable=True)
    stockId                = Column("StockId",                String,     nullable=True, index=True)
    changeType             = Column("ChangeType",             String,     nullable=False)
    previousValue          = Column("PreviousValue",          Integer,    nullable=True)
    newValue               = Column("NewValue",               Integer,    nullable=True)
    totalStock             = Column("TotalStock",             Integer,    nullable=True)
    processedQuantity      = Column("ProcessedQuantity",      Integer,    nullable=True)
    returnedQuantity       = Column("ReturnedQuantity",       Integer,    nullable=True)
    availableQuantity      = Column("AvailableQuantity",      Integer,    nullable=True)
    note                   = Column("Note",                   String,     nullable=True)
    changedBy              = Column("ChangedBy",              String,     nullable=True)
    createdDate            = Column("CreatedDate",            DateTime(timezone=True), server_default=func.now())


class ProductAuditLog(Base):
    """
    Product lifecycle audit trail — every structural and stock event.
    Lives in stocklog module so it is auto-created with the existing
    StockLog table (already imported in main.py — no main.py changes needed).

    Event types
    -----------
    PRODUCT_CREATED  PRODUCT_DELETED  PRODUCT_UPDATED
    VARIANT_ADDED    VARIANT_DELETED
    SIZE_ADDED       SIZE_DELETED
    STOCK_UPDATED

    Event sources
    -------------
    ProductDetail | BulkUpload | StockPage | OrderSystem | System
    """
    __tablename__ = "ProductAuditLog"
    __table_args__ = {"schema": "twam", "extend_existing": True}

    productAuditLogId       = Column("ProductAuditLogId",       BigInteger, primary_key=True, index=True, autoincrement=True)
    productId               = Column("ProductId",               BigInteger, nullable=False, index=True)
    productVariantId        = Column("ProductVariantId",        BigInteger, nullable=True)
    productVariantDetailId  = Column("ProductVariantDetailId",  BigInteger, nullable=True)

    eventType    = Column("EventType",    String,  nullable=False)
    eventSource  = Column("EventSource",  String,  nullable=True)
    description  = Column("Description", String,  nullable=True)
    stockBefore  = Column("StockBefore", Integer, nullable=True)
    stockAfter   = Column("StockAfter",  Integer, nullable=True)
    changedBy    = Column("ChangedBy",   String,  nullable=True)

    # Denormalized — fast reads without joins
    productCode  = Column("ProductCode", String, nullable=True)
    productName  = Column("ProductName", String, nullable=True)
    variantName  = Column("VariantName", String, nullable=True)
    sizeLabel    = Column("SizeLabel",   String, nullable=True)
    stockId      = Column("StockId",     String, nullable=True)

    createdDate  = Column("CreatedDate", DateTime(timezone=True), server_default=func.now(), index=True)
