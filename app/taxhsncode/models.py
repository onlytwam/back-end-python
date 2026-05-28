from sqlalchemy import Column, BigInteger, String, Boolean, DateTime, Numeric, Integer
from database import Base


class TaxHSNCode(Base):
    """
    Mirrors mdm."TaxHSNCode" exactly.

    Actual DB columns (verified from CSV export):
      TaxHSNCodeId, HSNCode, HSNDescription, IsActive, DeletedInd,
      CreatedDate, ModifiedDate, CreatedBy, ModifiedBy, OrganizationId,
      Slab1..Slab3 fields.

    Columns removed vs original model (did NOT exist in DB → caused the crash):
      - UserProfileId
      - State

    Columns restored vs original model (DO exist in DB):
      - CreatedBy, ModifiedBy, OrganizationId
    """
    __tablename__ = "TaxHSNCode"
    __table_args__ = {"schema": "mdm"}

    taxHSNCodeId   = Column("TaxHSNCodeId",   BigInteger, primary_key=True, index=True, autoincrement=True)
    hsnCode        = Column("HSNCode",         String,     nullable=True)
    hsnDescription = Column("HSNDescription",  String,     nullable=True)
    isActive       = Column("IsActive",        Boolean,    default=True)
    deletedInd     = Column("DeletedInd",      Boolean,    default=False)
    createdDate    = Column("CreatedDate",      DateTime(timezone=True), nullable=True)
    modifiedDate   = Column("ModifiedDate",     DateTime(timezone=True), nullable=True)
    createdBy      = Column("CreatedBy",        String,     nullable=True)
    modifiedBy     = Column("ModifiedBy",       String,     nullable=True)
    organizationId = Column("OrganizationId",   Integer,    nullable=True)

    # ── Slab 1 (required – lowest price range) ────────────────
    slab1MinPrice    = Column("Slab1MinPrice",    Numeric(22, 6), nullable=True)
    slab1MaxPrice    = Column("Slab1MaxPrice",    Numeric(22, 6), nullable=True)  # NULL = no upper limit
    slab1CGST        = Column("Slab1CGST",        Numeric(22, 6), nullable=True)
    slab1SGST        = Column("Slab1SGST",        Numeric(22, 6), nullable=True)
    slab1TotalGST    = Column("Slab1TotalGST",    Numeric(22, 6), nullable=True)
    slab1Description = Column("Slab1Description", String,         nullable=True)

    # ── Slab 2 ────────────────────────────────────────────────
    slab2MinPrice    = Column("Slab2MinPrice",    Numeric(22, 6), nullable=True)
    slab2MaxPrice    = Column("Slab2MaxPrice",    Numeric(22, 6), nullable=True)
    slab2CGST        = Column("Slab2CGST",        Numeric(22, 6), nullable=True)
    slab2SGST        = Column("Slab2SGST",        Numeric(22, 6), nullable=True)
    slab2TotalGST    = Column("Slab2TotalGST",    Numeric(22, 6), nullable=True)
    slab2Description = Column("Slab2Description", String,         nullable=True)

    # ── Slab 3 (optional) ─────────────────────────────────────
    slab3MinPrice    = Column("Slab3MinPrice",    Numeric(22, 6), nullable=True)
    slab3MaxPrice    = Column("Slab3MaxPrice",    Numeric(22, 6), nullable=True)
    slab3CGST        = Column("Slab3CGST",        Numeric(22, 6), nullable=True)
    slab3SGST        = Column("Slab3SGST",        Numeric(22, 6), nullable=True)
    slab3TotalGST    = Column("Slab3TotalGST",    Numeric(22, 6), nullable=True)
    slab3Description = Column("Slab3Description", String,         nullable=True)