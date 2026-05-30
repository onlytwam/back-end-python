from sqlalchemy import Column, BigInteger, Integer, String, DateTime
from sqlalchemy.sql import func
from database import Base


class ProductAuditLog(Base):
    """
    Tracks every structural and stock event across the product lifecycle.
    Covers: product / variant / size (PVD) creation, deletion, and stock changes.
    """
    __tablename__ = "ProductAuditLog"
    __table_args__ = {"schema": "twam", "extend_existing": True}

    productAuditLogId       = Column("ProductAuditLogId",       BigInteger, primary_key=True, index=True, autoincrement=True)
    productId               = Column("ProductId",               BigInteger, nullable=False,   index=True)
    productVariantId        = Column("ProductVariantId",        BigInteger, nullable=True)
    productVariantDetailId  = Column("ProductVariantDetailId",  BigInteger, nullable=True)

    # PRODUCT_CREATED | PRODUCT_DELETED | PRODUCT_UPDATED
    # VARIANT_ADDED   | VARIANT_DELETED
    # SIZE_ADDED      | SIZE_DELETED
    # STOCK_UPDATED
    eventType    = Column("EventType",    String, nullable=False)

    # ProductDetail | BulkUpload | StockPage | OrderSystem | System
    eventSource  = Column("EventSource",  String, nullable=True)

    description  = Column("Description", String, nullable=True)
    stockBefore  = Column("StockBefore", Integer, nullable=True)
    stockAfter   = Column("StockAfter",  Integer, nullable=True)
    changedBy    = Column("ChangedBy",   String,  nullable=True)

    # Denormalized for fast history queries (avoids joins on read)
    productCode  = Column("ProductCode", String, nullable=True)
    productName  = Column("ProductName", String, nullable=True)
    variantName  = Column("VariantName", String, nullable=True)
    sizeLabel    = Column("SizeLabel",   String, nullable=True)
    stockId      = Column("StockId",     String, nullable=True)

    createdDate  = Column("CreatedDate", DateTime(timezone=True), server_default=func.now(), index=True)
