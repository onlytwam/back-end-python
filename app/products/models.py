from sqlalchemy import Column, Integer, BigInteger, String, Boolean, DateTime, ForeignKey
from sqlalchemy.sql import func
from database import Base


class Products(Base):
    __tablename__ = "Products"
    __table_args__ = {"schema": "twam", "extend_existing": True}

    productId       = Column("ProductId",       BigInteger, primary_key=True, index=True, autoincrement=True)
    productCode     = Column("ProductCode",      String,     nullable=True)
    name            = Column("Name",             String,     nullable=True)
    description     = Column("Description",      String,     nullable=True)
    categoryId      = Column("CategoryId",       BigInteger, nullable=True)
    childCategoryId = Column("ChildCategoryId",  BigInteger, nullable=True)
    brandId         = Column("BrandId",          BigInteger, nullable=True)
    personalId      = Column("PersonalId",       BigInteger, nullable=True)
    userProfileId   = Column("UserProfileId",    String,     nullable=True)
    tag             = Column("Tag",              String,     nullable=True)
    state           = Column("State",            String,     nullable=True)
    # HSN code belongs at product level — same GST classification applies
    # across all colour/size variants of the same product.
    # FK → mdm.TaxHSNCode.TaxHSNCodeId
    taxHSNCodeId    = Column("TaxHSNCodeId",     BigInteger, nullable=True)
    # Fabric belongs at product level — all colour/size variants share the
    # same fabric composition.  FK → mdm.Fabric.FabricId
    fabricId        = Column("FabricId",         BigInteger, nullable=True)
    createdBy       = Column("CreatedBy",        String,     nullable=True)
    modifiedBy      = Column("ModifiedBy",       String,     nullable=True)
    createdDate     = Column("CreatedDate",      DateTime(timezone=True), server_default=func.now())
    modifiedDate    = Column("ModifiedDate",     DateTime(timezone=True), onupdate=func.now(), nullable=True)
    isActive        = Column("IsActive",         Boolean,    default=True)
    deletedInd      = Column("DeletedInd",       Boolean,    default=False)