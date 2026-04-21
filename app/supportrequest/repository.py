from datetime import datetime, timezone
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import asc, desc
from .models import SupportRequest


# ─────────────────────────────────────────────
# Helper: Convert model → dict
# ─────────────────────────────────────────────
def _to_dict(r: SupportRequest):
    return {
        "supportId": r.supportId,
        "name": r.name,
        "email": r.email,
        "subject": r.subject,
        "message": r.message,
        "remarks": r.remarks,
        "state": r.state,
        "createdDate": r.createdDate,
        "modifiedDate": r.modifiedDate,
    }


# ─────────────────────────────────────────────
# Get All Support Requests
# ─────────────────────────────────────────────
def get_all(
    db: Session,
    filters: Optional[List[dict]],
    order_ascending: Optional[bool],
    order_property: Optional[str],
    page_index: Optional[int],
    page_size: Optional[int],
):
    query = db.query(SupportRequest).filter(SupportRequest.deletedInd == False)

    if filters:
        for f in filters:
            col = getattr(SupportRequest, f.get("property", ""), None)
            if col is not None:
                query = query.filter(col == f.get("value"))

    if order_property:
        col = getattr(SupportRequest, order_property, None)
        if col is not None:
            query = query.order_by(
                asc(col) if order_ascending is not False else desc(col)
            )

    total = query.count()

    if page_index and page_size:
        offset = (page_index - 1) * page_size
        query = query.offset(offset).limit(page_size)

    data = query.all()

    return {
        "count": total,
        "list": [_to_dict(r) for r in data],
        "parameters": None,
    }


# ─────────────────────────────────────────────
# Get By ID
# ─────────────────────────────────────────────
def get_by_id(db: Session, support_id: int):
    entity = db.query(SupportRequest).filter(
        SupportRequest.supportId == support_id,
        SupportRequest.deletedInd == False,
    ).first()

    return _to_dict(entity) if entity else None


# ─────────────────────────────────────────────
# Create
# ─────────────────────────────────────────────
def create(db: Session, data) -> int:
    entity = SupportRequest(
        name=data.name,
        email=data.email,
        subject=data.subject,
        message=data.message,
        remarks=data.remarks,
        state=getattr(data, "state", "OPEN"),
        createdDate=datetime.now(timezone.utc),
        deletedInd=False,
    )

    db.add(entity)
    db.commit()
    db.refresh(entity)

    return entity.supportId


# ─────────────────────────────────────────────
# Update
# ─────────────────────────────────────────────
def update(db: Session, data) -> Optional[int]:
    if not data.supportId:
        return None

    entity = db.query(SupportRequest).filter(
        SupportRequest.supportId == data.supportId,
        SupportRequest.deletedInd == False,
    ).first()

    if not entity:
        return None

    for field in ["name", "email", "subject", "message", "remarks", "state"]:
        if hasattr(data, field):
            setattr(entity, field, getattr(data, field))

    entity.modifiedDate = datetime.now(timezone.utc)

    db.commit()

    return entity.supportId


# ─────────────────────────────────────────────
# Delete (Soft Delete)
# ─────────────────────────────────────────────
def delete(db: Session, support_id: int) -> bool:
    entity = db.query(SupportRequest).filter(
        SupportRequest.supportId == support_id,
        SupportRequest.deletedInd == False,
    ).first()

    if not entity:
        return False

    entity.deletedInd = True
    entity.modifiedDate = datetime.now(timezone.utc)

    db.commit()

    return True