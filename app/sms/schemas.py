"""
app/sms/schemas.py
───────────────────
Pydantic schemas for the sms module.

Includes:
  - SmsMessageTemplate CRUD schemas  (moved from app/messagetemplate/schemas.py)
  - SmsCredential CRUD schemas       (new)
"""

from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel


# ── SmsMessageTemplate ────────────────────────────────────────────────────────

class MessageTemplateCreate(BaseModel):
    messageType:    Optional[str] = None
    messageContent: Optional[str] = None
    state:          Optional[str] = None
    dltId:          Optional[str] = None
    templateId:     Optional[str] = None


class MessageTemplateUpdate(BaseModel):
    messageTemplateId: int
    messageType:    Optional[str] = None
    messageContent: Optional[str] = None
    state:          Optional[str] = None
    dltId:          Optional[str] = None
    templateId:     Optional[str] = None


class MessageTemplateResponse(BaseModel):
    messageTemplateId: int
    messageType:    Optional[str]      = None
    messageContent: Optional[str]      = None
    state:          Optional[str]      = None
    dltId:          Optional[str]      = None
    templateId:     Optional[str]      = None
    createdDate:    Optional[datetime] = None

    class Config:
        from_attributes = True


class MessageTemplateListResponse(BaseModel):
    count:      int
    list:       List[MessageTemplateResponse]
    parameters: Optional[Any] = None


# ── SmsCredential ─────────────────────────────────────────────────────────────

class SmsCredentialCreate(BaseModel):
    apiUrl:     str
    apiMultiUrl: Optional[str] = None
    username:   str
    password:   str
    senderId:   str
    unicode:    str = "false"


class SmsCredentialUpdate(BaseModel):
    smsCredentialId: int
    apiUrl:          Optional[str] = None
    apiMultiUrl:     Optional[str] = None
    username:        Optional[str] = None
    password:        Optional[str] = None
    senderId:        Optional[str] = None
    unicode:         Optional[str] = None
    isActive:        Optional[bool] = None


class SmsCredentialResponse(BaseModel):
    smsCredentialId: int
    apiUrl:     str
    apiMultiUrl: Optional[str] = None
    username:   str
    password:   str
    senderId:   str
    unicode:    str
    isActive:   bool
    createdDate: Optional[datetime] = None

    class Config:
        from_attributes = True