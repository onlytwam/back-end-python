from sqlalchemy import Column, BigInteger, Integer, String, Boolean, DateTime, Text
from database import Base


class PageImage(Base):
    """
    Stores the URL for every page-specific image in the application.
    Each row is identified by a unique `page_key` string (e.g. "login_background",
    "home_section_twam"). The frontend fetches the URL via GET /api/images/{page_key}
    and admins upload replacements via PUT /api/images/{page_key}.
    """

    __tablename__ = "PageImages"
    __table_args__ = {"schema": "twam"}

    pageImageId   = Column("PageImageId",   BigInteger, primary_key=True, autoincrement=True)
    pageKey       = Column("PageKey",       String,     unique=True, nullable=False, index=True)
    imageUrl      = Column("ImageUrl",      Text,       nullable=True)
    description   = Column("Description",  Text,       nullable=True)
    isActive      = Column("IsActive",      Boolean,    default=True)
    createdDate   = Column("CreatedDate",   DateTime(timezone=True), nullable=True)
    modifiedDate  = Column("ModifiedDate",  DateTime(timezone=True), nullable=True)
    deletedInd    = Column("DeletedInd",    Boolean,    default=False)
    createdBy     = Column("CreatedBy",     String,     nullable=True)
    modifiedBy    = Column("ModifiedBy",    String,     nullable=True)
    organizationId = Column("OrganizationId", Integer,  nullable=True)
