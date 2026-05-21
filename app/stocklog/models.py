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
    # Barcode / unique identifier copied from ProductVariantDetail.StockId
    stockId                = Column("StockId",                String,     nullable=True, index=True)
    changeType             = Column("ChangeType",             String,     nullable=False)  # e.g. INITIAL_STOCK, TOTAL_STOCK, PROCESSED, RETURNED, AVAILABLE
    previousValue          = Column("PreviousValue",          Integer,    nullable=True)
    newValue               = Column("NewValue",               Integer,    nullable=True)
    totalStock             = Column("TotalStock",             Integer,    nullable=True)
    processedQuantity      = Column("ProcessedQuantity",      Integer,    nullable=True)
    returnedQuantity       = Column("ReturnedQuantity",       Integer,    nullable=True)
    availableQuantity      = Column("AvailableQuantity",      Integer,    nullable=True)
    note                   = Column("Note",                   String,     nullable=True)
    changedBy              = Column("ChangedBy",              String,     nullable=True)
    createdDate            = Column("CreatedDate",            DateTime(timezone=True), server_default=func.now())