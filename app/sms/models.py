"""
app/sms/models.py
──────────────────
All SQLAlchemy models for the sms schema.

Tables (all created by Base.metadata.create_all at startup):
  sms.SmsCredential       — API credentials for the SMS gateway
  sms.SmsMessageTemplate  — DLT-approved message templates
  sms.SmsLog              — audit log of every outbound SMS

SmsCredential replaces the SMS_API_* keys that previously lived in
twam.AppSettings.  notifications.py and the health-check endpoint now
read from this table instead of AppSettings.
"""

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from database import Base


class SmsCredential(Base):
    """
    SMS gateway API credentials.
    One active row per organisation.  Managed via /api/SmsCredential endpoints.

    Field mapping from the old twam.AppSettings keys:
        apiUrl      ← SMS_API_URL
        apiMultiUrl ← SMS_API_MULTI_URL
        username    ← SMS_API_USERNAME
        password    ← SMS_API_PASSWORD
        senderId    ← SMS_SENDER_ID
        unicode     ← SMS_UNICODE
    """
    __tablename__  = "SmsCredential"
    __table_args__ = {"schema": "sms"}

    smsCredentialId = Column("SmsCredentialId", BigInteger, primary_key=True, autoincrement=True)
    apiUrl          = Column("ApiUrl",          String,     nullable=False)
    apiMultiUrl     = Column("ApiMultiUrl",     String,     nullable=True)
    username        = Column("Username",        String,     nullable=False)
    password        = Column("Password",        String,     nullable=False)
    senderId        = Column("SenderId",        String(10), nullable=False)   # 6-char DLT header
    unicode         = Column("Unicode",         String(5),  nullable=False,  default="false")
    isActive        = Column("IsActive",        Boolean,    nullable=False,  default=True)
    createdDate     = Column("CreatedDate",     DateTime(timezone=True), server_default=func.now())
    modifiedDate    = Column("ModifiedDate",    DateTime(timezone=True), nullable=True, onupdate=func.now())
    deletedInd      = Column("DeletedInd",      Boolean,    nullable=False,  default=False)
    createdBy       = Column("CreatedBy",       String,     nullable=True)
    modifiedBy      = Column("ModifiedBy",      String,     nullable=True)
    organizationId  = Column("OrganizationId",  Integer,    nullable=True,   default=1)

    def __repr__(self):
        return f"<SmsCredential(id={self.smsCredentialId}, sender={self.senderId!r})>"


class SmsMessageTemplate(Base):
    """
    DLT-approved SMS templates.

    Column semantics (identical attribute names to old mdm.MessageTemplate
    so all existing code keeps working):
        messageTemplateId — surrogate PK
        messageType       — e.g. ORDER_PLACED, OTP  (unique)
        messageContent    — body with {variable_name} placeholders
        state             — 'Active' (sends SMS) | 'Inactive' (not yet DLT-approved)
        dltId             — Principal Entity ID   → sent as dltPrincipalEntityId in API
        templateId        — DLT Content/Template ID → sent as dltContentId in API
    """
    __tablename__  = "SmsMessageTemplate"
    __table_args__ = {"schema": "sms"}

    messageTemplateId = Column("MessageTemplateId", BigInteger, primary_key=True, autoincrement=True, index=True)
    messageType       = Column("MessageType",       String,     nullable=True,  unique=True, index=True)
    messageContent    = Column("MessageContent",    Text,       nullable=True)
    state             = Column("State",             String,     nullable=True,  default="Active")
    dltId             = Column("DLTId",             String,     nullable=True)
    templateId        = Column("TemplateId",        String,     nullable=True)
    createdDate       = Column("CreatedDate",       DateTime(timezone=True), nullable=True, server_default=func.now())
    modifiedDate      = Column("ModifiedDate",      DateTime(timezone=True), nullable=True, onupdate=func.now())
    deletedInd        = Column("DeletedInd",        Boolean,    nullable=False,  default=False)
    createdBy         = Column("CreatedBy",         String,     nullable=True)
    modifiedBy        = Column("ModifiedBy",        String,     nullable=True)
    organizationId    = Column("OrganizationId",    Integer,    nullable=True,   default=1)

    def __repr__(self):
        return f"<SmsMessageTemplate(type={self.messageType!r}, state={self.state!r})>"


class SmsLog(Base):
    """
    Audit log for every outbound SMS — success and failure alike.
    Written by notifications._send_sms() after every gateway API call.
    Mirrors email.Email so SMS delivery is tracked the same way.
    """
    __tablename__  = "SmsLog"
    __table_args__ = {"schema": "sms"}

    smsLogId             = Column("SmsLogId",             BigInteger,  primary_key=True, autoincrement=True)
    recipient            = Column("Recipient",            String(20),  nullable=False, index=True)
    messageType          = Column("MessageType",          String(100), nullable=True,  index=True)
    messageContent       = Column("MessageContent",       Text,        nullable=True)
    templateId           = Column("TemplateId",           String(50),  nullable=True)
    dltPrincipalEntityId = Column("DltPrincipalEntityId", String(50),  nullable=True)
    transactionId        = Column("TransactionId",        String(50),  nullable=True)
    state                = Column("State",                String(50),  nullable=True)
    statusCode           = Column("StatusCode",           Integer,     nullable=True)
    errorDesc            = Column("ErrorDesc",            Text,        nullable=True)
    sentAt               = Column("SentAt",               DateTime(timezone=True), server_default=func.now(), index=True)
    deliveredAt          = Column("DeliveredAt",          DateTime(timezone=True), nullable=True)
    deletedInd           = Column("DeletedInd",           Boolean,     nullable=False, default=False)
    organizationId       = Column("OrganizationId",       Integer,     nullable=True,  default=1)

    def __repr__(self):
        return f"<SmsLog(id={self.smsLogId}, recipient={self.recipient!r}, state={self.state!r})>"