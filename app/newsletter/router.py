from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy.orm import Session
from database import get_db
from app.newsletter import repository
from app.newsletter.repository import API_BASE, FRONTEND_BASE
from app.newsletter.schemas import NewsletterSubscribe, NewsletterUnsubscribe, NewsletterBlast
from app.auth.dependencies import require_roles, Roles

router = APIRouter(prefix="/api/Newsletter", tags=["Newsletter"])


# ── Subscribe ─────────────────────────────────────────────────────────────────

@router.post("/Subscribe")
def subscribe(payload: NewsletterSubscribe, db: Session = Depends(get_db)):
    """
    Subscribe by email and/or phone number.
    - email only        → welcome email sent
    - phone only        → welcome WhatsApp sent
    - email (registered user with phone in account) → welcome email + WhatsApp
    - email + phone     → welcome email + WhatsApp
    Returns: { "status": "new" | "already_subscribed" | "resubscribed" }
    """
    status = repository.subscribe(db, email=payload.email, phone=payload.phone)
    return {"status": status}


# ── Unsubscribe via GET — from email link ─────────────────────────────────────

@router.get("/Unsubscribe", response_class=HTMLResponse)
def unsubscribe_via_link(token: str, db: Session = Depends(get_db)):
    import base64
    try:
        email = base64.b64decode(token).decode()
        assert "@" in email
    except Exception:
        return _page("❌", "#A84C6B", "Invalid Link",
                     "This unsubscribe link is invalid or has expired.")

    result = repository.unsubscribe(db, email)
    return _page("✓", "#6B0F2A",
                 "You have been unsubscribed",
                 f"<strong>{email}</strong> has been successfully removed from our marketing emails.<br><br>"
                 "You will still receive order confirmations and important account updates.")


# ── Unsubscribe via POST ──────────────────────────────────────────────────────

@router.post("/Unsubscribe")
def unsubscribe_api(
    token: str = None,
    payload: NewsletterUnsubscribe = None,
    db: Session = Depends(get_db),
):
    import base64
    if token:
        try:
            email = base64.b64decode(token).decode()
            repository.unsubscribe(db, email)
            return {"status": "unsubscribed"}
        except Exception:
            return {"status": "invalid_token"}
    if payload and payload.email:
        found = repository.unsubscribe(db, payload.email)
        return {"status": "unsubscribed" if found else "not_found"}
    return {"status": "error"}


# ── Check ─────────────────────────────────────────────────────────────────────

@router.get("/Check")
def check(email: str, db: Session = Depends(get_db)):
    return {"email": email, "isSubscribed": repository.check_subscribed(db, email)}


# ── List subscribers (admin) ──────────────────────────────────────────────────

@router.get("/Subscribers", dependencies=[Depends(require_roles(Roles.SUPER_ADMIN))])
def list_subscribers(db: Session = Depends(get_db)):
    """
    Returns separate email list and WhatsApp list.
    email_list  — all subscribers with email stored
    wa_list     — all subscribers with phone stored
    """
    data = repository.list_active_subscribers(db)
    return {
        "total":          data["total"],
        "email_count":    data["email_count"],
        "whatsapp_count": data["wa_count"],
        "email_list":     data["email_list"],
        "wa_list":        data["wa_list"],
    }


# ── Blast (admin) ─────────────────────────────────────────────────────────────

@router.post("/Blast", dependencies=[Depends(require_roles(Roles.SUPER_ADMIN))])
def send_blast(payload: NewsletterBlast, db: Session = Depends(get_db)):
    """
    Admin: Send blast to all active subscribers.
    channels: "email" | "whatsapp" | "both"
    media_url: optional public image URL for WhatsApp
    """
    result = repository.send_blast(
        db,
        subject   = payload.subject,
        body_html = payload.body_html,
        sent_by   = payload.sent_by,
        media_url = payload.media_url,
        channels  = payload.channels or "both",
    )
    return result


# ── HTML page helper ──────────────────────────────────────────────────────────

def _page(icon: str, color: str, title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{title} — TWAM</title>
  <style>
    *{{margin:0;padding:0;box-sizing:border-box}}
    body{{min-height:100vh;display:flex;align-items:center;justify-content:center;
         background:#F5F0E6;font-family:'Trebuchet MS',Arial,sans-serif;padding:20px}}
    .card{{background:#fff;border-radius:16px;padding:48px 40px;max-width:460px;
           width:100%;text-align:center;box-shadow:0 8px 32px rgba(107,15,42,.12);
           border:1px solid #e8d0d8}}
    .icon{{width:80px;height:80px;border-radius:50%;background:rgba(107,15,42,.08);
           display:flex;align-items:center;justify-content:center;margin:0 auto 24px;font-size:40px}}
    h1{{color:{color};font-size:24px;font-weight:700;margin-bottom:12px}}
    p{{color:#555;font-size:14px;line-height:1.7;margin-bottom:8px}}
    .btn{{display:inline-block;margin-top:28px;padding:12px 36px;background:#6B0F2A;
          color:#fff;border-radius:8px;text-decoration:none;font-size:14px;font-weight:700}}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon" style="color:{color}">{icon}</div>
    <h1>{title}</h1>
    <p>{body}</p>
    <a class="btn" href="{FRONTEND_BASE}">Back to TWAM</a>
  </div>
</body>
</html>"""