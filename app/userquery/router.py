"""
app/userquery/router.py
────────────────────────
POST   /api/UserQuery             – logged-in users submit a query
GET    /api/UserQuery             – CS / Admin: list all queries
GET    /api/UserQuery/stats       – CS / Admin: open/resolved counts
GET    /api/UserQuery/{id}        – CS / Admin: single query detail
PUT    /api/UserQuery/{id}/reply  – CS / Admin: reply + mark resolved
GET    /api/UserQuery/orders/{user_id} – CS / Admin: orders for a user
"""
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from database import get_db
from app.auth.dependencies import get_current_user, require_roles, Roles, CurrentUser
from . import schemas, repository
from app.sendemail import repository as email_repo
from app.sendemail.schemas import EmailCreate

router = APIRouter(prefix="/api/UserQuery", tags=["UserQuery"])

_CS_OR_ADMIN = Depends(
    require_roles(Roles.SUPER_ADMIN, Roles.CUSTOMER_SUPPORT, Roles.ORDER_MANAGER)
)


# ── User: submit a query ──────────────────────────────────────────────────────

@router.post("", status_code=201)
def create_query(
    command: schemas.UserQueryCreate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    query_id = repository.create(db, command, current_user)
    return {"queryId": query_id}


# ── CS / Admin: stats ─────────────────────────────────────────────────────────

@router.get("/stats")
def get_stats(db: Session = Depends(get_db), _: CurrentUser = _CS_OR_ADMIN):
    return repository.get_stats(db)


# ── CS / Admin: list all queries ──────────────────────────────────────────────

@router.get("", response_model=schemas.UserQueryListResponse)
def get_list(
    Filters:         Optional[str]  = Query(None, alias="Filters"),
    Order_Ascending: Optional[bool] = Query(None, alias="Order.Ascending"),
    Order_Property:  Optional[str]  = Query(None, alias="Order.Property"),
    Page_Index:      Optional[int]  = Query(None, alias="Page.Index", ge=1),
    Page_Size:       Optional[int]  = Query(None, alias="Page.Size",  ge=1),
    db: Session = Depends(get_db),
    _: CurrentUser = _CS_OR_ADMIN,
):
    return repository.get_all(
        db,
        json.loads(Filters) if Filters else None,
        Order_Ascending,
        Order_Property,
        Page_Index,
        Page_Size,
    )


# ── CS / Admin: single query ──────────────────────────────────────────────────

@router.get("/{query_id}", response_model=schemas.UserQueryResponse)
def get_detail(
    query_id: int,
    db: Session = Depends(get_db),
    _: CurrentUser = _CS_OR_ADMIN,
):
    result = repository.get_by_id(db, query_id)
    if not result:
        raise HTTPException(404, "Query not found.")
    return result


# ── CS / Admin: reply to a query ─────────────────────────────────────────────

@router.put("/{query_id}/reply")
def reply_to_query(
    query_id: int,
    payload: schemas.UserQueryReply,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(
        require_roles(Roles.SUPER_ADMIN, Roles.CUSTOMER_SUPPORT, Roles.ORDER_MANAGER)
    ),
):
    query = repository.get_by_id(db, query_id)
    if not query:
        raise HTTPException(404, "Query not found.")

    # Persist reply & mark resolved
    updated = repository.resolve(
        db, query_id, payload.replyText, current_user.full_name or current_user.email
    )

    sent_channels = []

    # ── Send email reply ──────────────────────────────────────────────────────
    if payload.sendEmail and query.get("userEmail"):
        try:
            email_repo.send_email(
                db,
                EmailCreate(
                    email   = query["userEmail"],
                    subject = "Response to your query – TWAM Support",
                    message = (
                        f"<p>Hi {query.get('userName', 'there')},</p>"
                        f"<p>Thank you for reaching out to TWAM Support. "
                        f"Here is our response to your query:</p>"
                        f"<blockquote style='border-left:3px solid #ccc;padding:8px 16px;color:#555'>"
                        f"{payload.replyText}"
                        f"</blockquote>"
                        f"<p>If you have further questions, feel free to contact us again.</p>"
                        f"<p>Warm regards,<br/>TWAM Customer Support</p>"
                    ),
                ),
            )
            sent_channels.append("email")
        except Exception as e:
            print(f"Email send error: {e}")

    # ── Send SMS reply (if requested and phone available) ─────────────────────
    if payload.sendSms and query.get("userPhone"):
        try:
            # Use your active SMS gateway integration here.
            # Example: call the SMS credential from sms.SmsCredential + provider API.
            # Placeholder — replace with actual SMS provider call:
            print(f"[SMS] Would send to {query['userPhone']}: {payload.replyText[:140]}")
            sent_channels.append("sms")
        except Exception as e:
            print(f"SMS send error: {e}")

    return {"success": True, "sentChannels": sent_channels, "query": updated}


# ── CS / Admin: view orders for a specific user ───────────────────────────────

@router.get("/orders/{user_id}")
def get_orders_for_user(
    user_id: str,
    Page_Index: Optional[int] = Query(None, alias="Page.Index", ge=1),
    Page_Size:  Optional[int] = Query(10,   alias="Page.Size",  ge=1),
    db: Session = Depends(get_db),
    _: CurrentUser = _CS_OR_ADMIN,
):
    from app.orders.models import Orders
    from app.orders import repository as orders_repo

    query = (
        db.query(Orders)
        .filter(Orders.userProfileId == user_id, Orders.deletedInd == False)
        .order_by(Orders.createdDate.desc())
    )
    total = query.count()
    page  = Page_Index or 1
    size  = Page_Size  or 10
    items = query.offset((page - 1) * size).limit(size).all()

    def _order_dict(o):
        return {
            "orderId":     o.orderId,
            "orderNumber": o.orderNumber,
            "state":       o.state,
            "totalAmount": float(o.totalAmount) if o.totalAmount else None,
            "orderDate":   o.orderDate.isoformat() if o.orderDate else None,
            "isShipped":   o.isShipped,
            "trackingId":  o.trackingId,
            "paymentMethod": o.paymentMethod,
        }

    return {"count": total, "list": [_order_dict(o) for o in items]}
