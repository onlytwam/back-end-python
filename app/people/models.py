from sqlalchemy import Column, BigInteger, ForeignKey, Integer, String, Boolean, Date, DateTime
from database import Base
from sqlalchemy.orm import relationship

class People(Base):
    __tablename__ = "People"
    __table_args__ = {"schema": "twam"}

    personalId = Column("PersonalId", BigInteger, primary_key=True, index=True, autoincrement=True)
    firstName = Column("FirstName", String, nullable=True)
    middleName = Column("MiddleName", String, nullable=True)
    lastName = Column("LastName", String, nullable=True)
    dateOfBirth = Column("DateOfBirth", Date, nullable=True)
    emailId = Column("EmailId", String, nullable=True)
    phoneNumber = Column("PhoneNumber", String, nullable=True)
    profileImageUrl = Column("ProfileImageUrl", String, nullable=True)
    userProfileId = Column("UserProfileId", String, nullable=True)
    isActive = Column("IsActive", Boolean, nullable=True)
    roleId = Column("RoleId", Integer, nullable=True)
    createdDate = Column("CreatedDate", DateTime(timezone=True), nullable=True)
    modifiedDate = Column("ModifiedDate", DateTime(timezone=True), nullable=True)
    deletedInd = Column("DeletedInd", Boolean, default=False)
    createdBy      = Column("CreatedBy",      String,  nullable=True)
    modifiedBy     = Column("ModifiedBy",     String,  nullable=True)
    organizationId = Column("OrganizationId", Integer, nullable=True)
    StoreId = Column(BigInteger, ForeignKey("twam.Stores.StoreId"))
    WarehouseId = Column(BigInteger, ForeignKey("twam.Warehouses.WarehouseId"))

    store = relationship("Stores", back_populates="people")
    warehouse = relationship("Warehouse", back_populates="people")  
    def __repr__(self):
        return f"<People(PersonalId={self.personalId}, FirstName={self.firstName}, EmailId={self.emailId})>"            
