"""
app/sms/repository.py
──────────────────────
CRUD operations for:
  - sms.SmsMessageTemplate  (absorbed from app/messagetemplate/repository.py)
  - sms.SmsCredential       (new)
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import asc, desc
from sqlalchemy.orm import Session

from .models import SmsCredential, SmsMessageTemplate


# ─────────────────────────────────────────────────────────────────────────────
# SmsMessageTemplate
# ─────────────────────────────────────────────────────────────────────────────

def _template_to_dict(r: SmsMessageTemplate) -> dict:
    d = {"messageTemplateId": r.messageTemplateId, "createdDate": r.createdDate}
    for attr in ["messageType", "messageContent", "state", "dltId", "templateId"]:
        v = getattr(r, attr, None)
        d[attr] = (
            float(v)
            if v is not None
            and hasattr(v, "__float__")
            and not isinstance(v, (bool, int, str))
            else v
        )
    return d


def get_all_templates(
    db: Session, filters, order_ascending, order_property, page_index, page_size
):
    query = db.query(SmsMessageTemplate).filter(SmsMessageTemplate.deletedInd == False)
    if filters:
        for f in filters:
            col = getattr(SmsMessageTemplate, f.get("property", ""), None)
            if col is not None:
                query = query.filter(col == f.get("value"))
    if order_property:
        col = getattr(SmsMessageTemplate, order_property, None)
        if col:
            query = query.order_by(asc(col) if order_ascending is not False else desc(col))
    total = query.count()
    if page_index and page_size:
        query = query.offset((page_index - 1) * page_size).limit(page_size)
    return {"count": total, "list": [_template_to_dict(r) for r in query.all()], "parameters": None}


def get_template_by_id(db: Session, id_val: int) -> Optional[dict]:
    r = db.query(SmsMessageTemplate).filter(
        SmsMessageTemplate.messageTemplateId == id_val,
        SmsMessageTemplate.deletedInd == False,
    ).first()
    return _template_to_dict(r) if r else None


def create_template(db: Session, data) -> int:
    entity = SmsMessageTemplate(
        messageType    = data.messageType,
        messageContent = data.messageContent,
        state          = data.state,
        dltId          = data.dltId,
        templateId     = data.templateId,
        createdDate    = datetime.now(timezone.utc),
        deletedInd     = False,
    )
    db.add(entity)
    db.commit()
    db.refresh(entity)
    return entity.messageTemplateId


def update_template(db: Session, data) -> Optional[int]:
    entity = db.query(SmsMessageTemplate).filter(
        SmsMessageTemplate.messageTemplateId == data.messageTemplateId
    ).first()
    if not entity:
        return None
    entity.messageType    = data.messageType
    entity.messageContent = data.messageContent
    entity.state          = data.state
    entity.dltId          = data.dltId
    entity.templateId     = data.templateId
    entity.modifiedDate   = datetime.now(timezone.utc)
    db.commit()
    return entity.messageTemplateId


def delete_template(db: Session, id_val: int) -> bool:
    entity = db.query(SmsMessageTemplate).filter(
        SmsMessageTemplate.messageTemplateId == id_val
    ).first()
    if not entity:
        return False
    entity.deletedInd   = True
    entity.modifiedDate = datetime.now(timezone.utc)
    db.commit()
    return True


# ─────────────────────────────────────────────────────────────────────────────
# SmsCredential
# ─────────────────────────────────────────────────────────────────────────────

def get_active_credential(db: Session) -> Optional[SmsCredential]:
    """Return the single active credential row, or None."""
    return db.query(SmsCredential).filter(
        SmsCredential.isActive  == True,
        SmsCredential.deletedInd == False,
    ).first()


def get_all_credentials(db: Session) -> list:
    return db.query(SmsCredential).filter(SmsCredential.deletedInd == False).all()


def get_credential_by_id(db: Session, id_val: int) -> Optional[SmsCredential]:
    return db.query(SmsCredential).filter(
        SmsCredential.smsCredentialId == id_val,
        SmsCredential.deletedInd == False,
    ).first()


def create_credential(db: Session, data) -> int:
    entity = SmsCredential(
        apiUrl      = data.apiUrl,
        apiMultiUrl = data.apiMultiUrl,
        username    = data.username,
        password    = data.password,
        senderId    = data.senderId,
        unicode     = data.unicode,
        isActive    = True,
        createdDate = datetime.now(timezone.utc),
        deletedInd  = False,
    )
    db.add(entity)
    db.commit()
    db.refresh(entity)
    return entity.smsCredentialId


def update_credential(db: Session, data) -> Optional[int]:
    entity = db.query(SmsCredential).filter(
        SmsCredential.smsCredentialId == data.smsCredentialId
    ).first()
    if not entity:
        return None
    if data.apiUrl     is not None: entity.apiUrl     = data.apiUrl
    if data.apiMultiUrl is not None: entity.apiMultiUrl = data.apiMultiUrl
    if data.username   is not None: entity.username   = data.username
    if data.password   is not None: entity.password   = data.password
    if data.senderId   is not None: entity.senderId   = data.senderId
    if data.unicode    is not None: entity.unicode    = data.unicode
    if data.isActive   is not None: entity.isActive   = data.isActive
    entity.modifiedDate = datetime.now(timezone.utc)
    db.commit()
    return entity.smsCredentialId


def delete_credential(db: Session, id_val: int) -> bool:
    entity = db.query(SmsCredential).filter(
        SmsCredential.smsCredentialId == id_val
    ).first()
    if not entity:
        return False
    entity.deletedInd   = True
    entity.modifiedDate = datetime.now(timezone.utc)
    db.commit()
    return True