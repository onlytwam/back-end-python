from datetime import datetime, timezone
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import asc, desc, func

from .models import UserQuery
from app.auth.models import IdentityUser


def _to_dict(q: UserQuery) -> dict:
    return {
        "queryId":       q.queryId,
        "message":       q.message,
        "attachmentUrl": q.attachmentUrl,
        "userProfileId": q.userProfileId,
        "userName":      q.userName,
        "userEmail":     q.userEmail,
        "userPhone":     q.userPhone,
        "state":         q.state,
        "replyText":     q.replyText,
        "repliedAt":     q.repliedAt,
        "repliedBy":     q.repliedBy,
        "createdDate":   q.createdDate,
    }


def get_all(
    db: Session,
    filters,
    order_ascending,
    order_property,
    page_index,
    page_size,
):
    query = db.query(UserQuery).filter(UserQuery.deletedInd == False)

    if filters:
        for f in filters:
            prop = f.get("property", "")
            val = f.get("value")
            col = getattr(UserQuery, prop, None)
            if col is None:
                col = getattr(UserQuery, prop[0].lower() + prop[1:] if prop else "", None)
            if col is not None:
                query = query.filter(col == val)

    if order_property:
        col = getattr(UserQuery, order_property, None)
        if col:
            query = query.order_by(asc(col) if order_ascending is not False else desc(col))
    else:
        query = query.order_by(desc(UserQuery.createdDate))

    total = query.count()
    if page_index and page_size:
        query = query.offset((page_index - 1) * page_size).limit(page_size)

    return {"count": total, "list": [_to_dict(q) for q in query.all()], "parameters": None}


def get_stats(db: Session) -> dict:
    """Return open / resolved counts."""
    total    = db.query(func.count(UserQuery.queryId)).filter(UserQuery.deletedInd == False).scalar()
    open_c   = db.query(func.count(UserQuery.queryId)).filter(UserQuery.deletedInd == False, UserQuery.state == "Open").scalar()
    resolved = db.query(func.count(UserQuery.queryId)).filter(UserQuery.deletedInd == False, UserQuery.state == "Resolved").scalar()
    return {"total": total, "open": open_c, "resolved": resolved}


def get_by_id(db: Session, query_id: int):
    q = db.query(UserQuery).filter(
        UserQuery.queryId == query_id,
        UserQuery.deletedInd == False,
    ).first()
    return _to_dict(q) if q else None


def create(db: Session, data, current_user) -> int:
    """Create a new query, pulling user details from IdentityUser."""
    identity = db.query(IdentityUser).filter(IdentityUser.Id == current_user.user_id).first()
    phone = identity.PhoneNumber if identity else None
    full_name = current_user.full_name or (
        f"{identity.FirstName or ''} {identity.LastName or ''}".strip() if identity else ""
    )

    entity = UserQuery(
        message       = data.message,
        attachmentUrl = data.attachmentUrl,
        userProfileId = current_user.user_id,
        userName      = full_name or current_user.email,
        userEmail     = current_user.email,
        userPhone     = phone,
        state         = "Open",
        deletedInd    = False,
    )
    db.add(entity)
    db.commit()
    db.refresh(entity)
    return entity.queryId


def resolve(db: Session, query_id: int, reply_text: str, replied_by: str) -> Optional[dict]:
    """Mark a query as Resolved and store the reply text."""
    entity = db.query(UserQuery).filter(UserQuery.queryId == query_id).first()
    if not entity:
        return None
    entity.state      = "Resolved"
    entity.replyText  = reply_text
    entity.repliedAt  = datetime.now(timezone.utc)
    entity.repliedBy  = replied_by
    entity.modifiedDate = datetime.now(timezone.utc)
    db.commit()
    return _to_dict(entity)
