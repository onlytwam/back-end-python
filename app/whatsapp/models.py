"""
app/whatsapp/models.py
──────────────────────
SQLAlchemy models for the whatsapp module.

Tables (in sms schema):
  sms.WhatsAppCredential  — API credentials (url, apikey) stored in DB, not hardcoded
  sms.WhatsAppLog         — audit log of every outbound WhatsApp message
"""

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func
from database import Base


class WhatsAppCredential(Base):
    """
    WhatsApp gateway API credentials.
    One active row per organisation. Managed via /api/WhatsApp/credentials endpoints.

    Stores:
        apiUrl   — e.g. http://apps.studentscorner.org/wapp/api/send
        apiKey   — e.g. 1db101c8cb594b39af22276010ad0c39
        timeout  — request timeout in seconds (default 10)
    """
    __tablename__  = "WhatsAppCredential"
    __table_args__ = {"schema": "sms"}

    whatsAppCredentialId = Column("WhatsAppCredentialId", BigInteger, primary_key=True, autoincrement=True)
    apiUrl               = Column("ApiUrl",               String,     nullable=False)
    apiKey               = Column("ApiKey",               String,     nullable=False)
    timeout              = Column("Timeout",              Integer,    nullable=False, default=10)
    isActive             = Column("IsActive",             Boolean,    nullable=False, default=True)
    createdDate          = Column("CreatedDate",          DateTime(timezone=True), server_default=func.now())
    modifiedDate         = Column("ModifiedDate",         DateTime(timezone=True), nullable=True, onupdate=func.now())
    deletedInd           = Column("DeletedInd",           Boolean,    nullable=False, default=False)
    createdBy            = Column("CreatedBy",            String,     nullable=True)
    modifiedBy           = Column("ModifiedBy",           String,     nullable=True)
    organizationId       = Column("OrganizationId",       Integer,    nullable=True, default=1)

    def __repr__(self):
        return f"<WhatsAppCredential(id={self.whatsAppCredentialId}, url={self.apiUrl!r})>"


class WhatsAppLog(Base):
    """
    Audit log for every outbound WhatsApp message — success and failure alike.
    Written by whatsapp.service.send_whatsapp() after every API call.
    """
    __tablename__  = "WhatsAppLog"
    __table_args__ = {"schema": "sms"}

    whatsAppLogId  = Column("WhatsAppLogId",  BigInteger,  primary_key=True, autoincrement=True)
    recipient      = Column("Recipient",      String(20),  nullable=False, index=True)
    messageType    = Column("MessageType",    String(100), nullable=True,  index=True)
    messageContent = Column("MessageContent", Text,        nullable=True)
    mediaUrl       = Column("MediaUrl",       String(500), nullable=True)
    transactionId  = Column("TransactionId",  String(100), nullable=True)
    state          = Column("State",          String(50),  nullable=True)
    statusCode     = Column("StatusCode",     Integer,     nullable=True)
    errorDesc      = Column("ErrorDesc",      Text,        nullable=True)
    sentAt         = Column("SentAt",         DateTime(timezone=True), server_default=func.now(), index=True)
    deletedInd     = Column("DeletedInd",     Boolean,     nullable=False, default=False)
    organizationId = Column("OrganizationId", Integer,     nullable=True,  default=1)

    def __repr__(self):
        return f"<WhatsAppLog(id={self.whatsAppLogId}, recipient={self.recipient!r}, state={self.state!r})>"