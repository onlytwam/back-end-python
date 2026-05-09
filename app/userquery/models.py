"""
app/userquery/models.py
────────────────────────
Replaces the old UserReview table entirely (drop & recreate via database.py).
Stores customer queries submitted from the Contact Us page.
"""

from sqlalchemy import Column, BigInteger, Integer, String, Boolean, DateTime, Text
from sqlalchemy.sql import func
from database import Base


class UserQuery(Base):
    __tablename__ = "UserQuery"
    __table_args__ = {"schema": "twam"}

    queryId        = Column("QueryId",        BigInteger, primary_key=True, index=True, autoincrement=True)
    message        = Column("Message",        Text,       nullable=True)
    attachmentUrl  = Column("AttachmentUrl",  String,     nullable=True)   # uploaded file URL
    userProfileId  = Column("UserProfileId",  String,     nullable=True)   # FK to IdentityUser.Id
    userName       = Column("UserName",       String,     nullable=True)   # snapshotted at submission
    userEmail      = Column("UserEmail",      String,     nullable=True)
    userPhone      = Column("UserPhone",      String,     nullable=True)
    state          = Column("State",          String,     default="Open")  # Open | Resolved
    replyText      = Column("ReplyText",      Text,       nullable=True)   # support reply content
    repliedAt      = Column("RepliedAt",      DateTime(timezone=True), nullable=True)
    repliedBy      = Column("RepliedBy",      String,     nullable=True)   # support staff name
    createdDate    = Column("CreatedDate",    DateTime(timezone=True), server_default=func.now())
    modifiedDate   = Column("ModifiedDate",   DateTime(timezone=True), onupdate=func.now(), nullable=True)
    deletedInd     = Column("DeletedInd",     Boolean,    default=False)
    organizationId = Column("OrganizationId", Integer,    nullable=True)
