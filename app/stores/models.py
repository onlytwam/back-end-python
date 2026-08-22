from sqlalchemy import Column, Integer, BigInteger, Boolean, DateTime, String
from database import Base
from sqlalchemy.orm import relationship


class Stores(Base):
    __tablename__ = "Stores"
    __table_args__ = {"schema": "twam"}
    storeId = Column("StoreId", BigInteger, primary_key=True, index=True, autoincrement=True)
    storeName = Column("StoreName", String, nullable=True)
    country = Column("Country", BigInteger, nullable=True)
    city = Column("City", String, nullable=True)
    pinCode = Column("PinCode", String, nullable=True)
    address = Column("Address", String, nullable=True)
    phone = Column("Phone", String, nullable=True)
    email = Column("Email", String, nullable=True)
    isPickUpAvailable = Column("IsPickUpAvailable", Boolean, nullable=True)
    mapLink = Column("MapLink", String, nullable=True)
    state = Column("State", String, nullable=True)
    isActive = Column("IsActive", Boolean, default=True)
    loginEmail = Column("LoginEmail", String, nullable=True)
    loginPassword = Column("LoginPassword", String, nullable=True)
    userProfileId = Column("UserProfileId", String, nullable=True)
    createdDate = Column("CreatedDate", DateTime(timezone=True), nullable=True)
    modifiedDate = Column("ModifiedDate", DateTime(timezone=True), nullable=True)
    deletedInd = Column("DeletedInd", Boolean, default=False)
    createdBy      = Column("CreatedBy",      String,  nullable=True)
    modifiedBy     = Column("ModifiedBy",     String,  nullable=True)
    organizationId = Column("OrganizationId", Integer, nullable=True)

    inventory = relationship("Inventory",back_populates="store")
    people = relationship("People", back_populates="store")
