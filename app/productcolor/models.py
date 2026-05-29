from sqlalchemy import Column, Integer, BigInteger, String, Boolean, DateTime
from sqlalchemy.sql import func
from database import Base


class ProductColor(Base):
    __tablename__ = "ProductColor"
    __table_args__ = {"schema": "mdm", "extend_existing": True}

    productColorId = Column("ProductColorId", BigInteger, primary_key=True, index=True, autoincrement=True)
    colorFamilyName = Column("ColorFamilyName", String, nullable=False)
    hexCode = Column("HexCode", String, nullable=False)
    description = Column("Description", String, nullable=True)
    orderNo = Column("OrderNo", Integer, nullable=True)
    state = Column("State", String, nullable=True)
    isActive = Column("IsActive", Boolean, default=True)
    createdDate = Column("CreatedDate", DateTime(timezone=True), server_default=func.now())
    modifiedDate = Column("ModifiedDate", DateTime(timezone=True), onupdate=func.now(), nullable=True)
    deletedInd = Column("DeletedInd", Boolean, default=False)
    createdBy = Column("CreatedBy", String, nullable=True)
    modifiedBy = Column("ModifiedBy", String, nullable=True)
    organizationId = Column("OrganizationId", Integer, nullable=True)
