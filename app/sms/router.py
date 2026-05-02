"""
app/sms/router.py
──────────────────
Exposes two route groups:

  /api/MessageTemplate  — CRUD for sms.SmsMessageTemplate
                          (same URL prefix as the old messagetemplate module,
                           so the frontend doesn't need any changes)

  /api/SmsCredential    — CRUD for sms.SmsCredential
                          (new — manage SMS gateway API credentials)
"""

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database import get_db
from app.auth.dependencies import get_current_user, require_roles, Roles, CurrentUser
from . import schemas, repository

# ── MessageTemplate router (same URL as the old module) ──────────────────────
template_router = APIRouter(
    dependencies=[Depends(get_current_user)],
    prefix="/api/MessageTemplate",
    tags=["MessageTemplate"],
)


@template_router.get("", response_model=schemas.MessageTemplateListResponse)
def get_list(
    Filters:          Optional[str]  = Query(None, alias="Filters"),
    Order_Ascending:  Optional[bool] = Query(None, alias="Order.Ascending"),
    Order_Property:   Optional[str]  = Query(None, alias="Order.Property"),
    Page_Index:       Optional[int]  = Query(None, alias="Page.Index", ge=1),
    Page_Size:        Optional[int]  = Query(None, alias="Page.Size",  ge=1),
    db: Session = Depends(get_db),
):
    return repository.get_all_templates(
        db,
        json.loads(Filters) if Filters else None,
        Order_Ascending,
        Order_Property,
        Page_Index,
        Page_Size,
    )


@template_router.get("/{messageTemplateId}", response_model=schemas.MessageTemplateResponse)
def get_detail(messageTemplateId: int, db: Session = Depends(get_db)):
    result = repository.get_template_by_id(db, messageTemplateId)
    if not result:
        raise HTTPException(404, "MessageTemplate not found.")
    return result


@template_router.post("", status_code=201)
def create(command: schemas.MessageTemplateCreate, db: Session = Depends(get_db)):
    return {"messageTemplateId": repository.create_template(db, command)}


@template_router.put("/{messageTemplateId}")
def update(
    messageTemplateId: int,
    command: schemas.MessageTemplateUpdate,
    db: Session = Depends(get_db),
):
    command.messageTemplateId = messageTemplateId
    result = repository.update_template(db, command)
    if not result:
        raise HTTPException(404, "MessageTemplate not found.")
    return {"messageTemplateId": result}


@template_router.delete("/{messageTemplateId}", status_code=204)
def delete(messageTemplateId: int, db: Session = Depends(get_db)):
    if not repository.delete_template(db, messageTemplateId):
        raise HTTPException(404, "MessageTemplate not found.")


# ── SmsCredential router ──────────────────────────────────────────────────────
credential_router = APIRouter(
    dependencies=[Depends(get_current_user)],
    prefix="/api/SmsCredential",
    tags=["SmsCredential"],
)


@credential_router.get("", response_model=list[schemas.SmsCredentialResponse])
def list_credentials(db: Session = Depends(get_db)):
    return repository.get_all_credentials(db)


@credential_router.get("/{smsCredentialId}", response_model=schemas.SmsCredentialResponse)
def get_credential(smsCredentialId: int, db: Session = Depends(get_db)):
    result = repository.get_credential_by_id(db, smsCredentialId)
    if not result:
        raise HTTPException(404, "SmsCredential not found.")
    return result


@credential_router.post("", status_code=201,
                         dependencies=[Depends(require_roles(Roles.SUPER_ADMIN))])
def create_credential(command: schemas.SmsCredentialCreate, db: Session = Depends(get_db)):
    return {"smsCredentialId": repository.create_credential(db, command)}


@credential_router.put("/{smsCredentialId}",
                        dependencies=[Depends(require_roles(Roles.SUPER_ADMIN))])
def update_credential(
    smsCredentialId: int,
    command: schemas.SmsCredentialUpdate,
    db: Session = Depends(get_db),
):
    command.smsCredentialId = smsCredentialId
    result = repository.update_credential(db, command)
    if not result:
        raise HTTPException(404, "SmsCredential not found.")
    return {"smsCredentialId": result}


@credential_router.delete("/{smsCredentialId}", status_code=204,
                           dependencies=[Depends(require_roles(Roles.SUPER_ADMIN))])
def delete_credential(smsCredentialId: int, db: Session = Depends(get_db)):
    if not repository.delete_credential(db, smsCredentialId):
        raise HTTPException(404, "SmsCredential not found.")