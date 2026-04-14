"""
app/newsletter/repository.py

Subscribe cases:
  Case 1 — phone only, not registered → save phone, WA welcome, WA list only
  Case 2 — email only, not registered → save email, email welcome, email list only
  Case 3 — email given, registered    → look up phone, save both, email+WA welcome, both lists
  Case 4 — phone given, registered    → look up email, save both, WA+email welcome, both lists
  Case 5 — both given explicitly      → save both, email+WA welcome, both lists

Duplicate handling:
  Before saving, detect conflicting existing rows (same email or phone in a DIFFERENT row).
  Merge them into one row (keep the one being processed, deactivate orphan).

Blast:
  email_list and wa_list are deduped with dict.fromkeys() preserving order.
"""
import base64
from datetime import datetime, timezone
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import text

from .models import Newsletter
from app.sendemail.repository import send_email
from app.sendemail.schemas import EmailCreate
from app.whatsapp.service import send_whatsapp


API_BASE      = "http://127.0.0.1:8000"
FRONTEND_BASE = "http://localhost:5173"


# ── Token / URL helpers ───────────────────────────────────────────────────────

def _token(email: str) -> str:
    return base64.b64encode(email.lower().strip().encode()).decode()

def _unsubscribe_url(email: str) -> str:
    return f"{API_BASE}/api/Newsletter/Unsubscribe?token={_token(email)}"

def wrap_marketing_email(recipient_email: str, body_html: str) -> str:
    unsub_url = _unsubscribe_url(recipient_email)
    return f"""
    <div style="max-width:600px;margin:0 auto;font-family:'Trebuchet MS',Arial,sans-serif;color:#1a0a10;">
      <div style="background:#f9f3f5;padding:8px 20px;text-align:right;border-bottom:1px solid #e8d0d8;">
        <a href="{unsub_url}"
           style="display:inline-block;padding:6px 18px;background:#6B0F2A;color:#ffffff;
                  font-size:12px;font-weight:600;text-decoration:none;border-radius:4px;">
          Unsubscribe
        </a>
      </div>
      <div style="background:#6B0F2A;padding:20px 32px;text-align:center;">
        <h2 style="color:#F5F0E6;margin:0;font-size:22px;letter-spacing:2px;">TWAM</h2>
      </div>
      <div style="padding:32px 24px;">{body_html}</div>
      <div style="margin-top:32px;padding-top:20px;border-top:1px solid #e8d0d8;
                  text-align:center;font-size:12px;color:#888;">
        <p style="margin:0 0 8px;">You subscribed to TWAM updates.</p>
        <a href="{unsub_url}"
           style="display:inline-block;padding:8px 22px;background:#6B0F2A;color:#fff;
                  font-size:12px;font-weight:600;text-decoration:none;border-radius:6px;">
          Unsubscribe
        </a>
        <p style="margin:8px 0 0;font-size:11px;color:#bbb;">
          TWAM &middot; GSM Mall, Miyapur Main Rd, Hyderabad 500050
        </p>
      </div>
    </div>"""


# ── People table lookups ──────────────────────────────────────────────────────

def _get_registered_phone(db: Session, email: str) -> Optional[str]:
    """Return 10-digit phone from twam.People for a given email, or None."""
    try:
        row = db.execute(text("""
            SELECT p."PhoneNumber" FROM twam."People" p
            WHERE  LOWER(p."EmailId") = LOWER(:email)
              AND  p."DeletedInd" = false
              AND  p."PhoneNumber" IS NOT NULL AND p."PhoneNumber" != ''
            LIMIT 1
        """), {"email": email}).fetchone()
        if not row:
            return None
        digits = "".join(c for c in row[0] if c.isdigit())
        return digits[-10:] if len(digits) >= 10 else None
    except Exception as e:
        print(f"[Newsletter] phone lookup by email failed: {e}")
        return None

def _get_registered_email(db: Session, phone: str) -> Optional[str]:
    """Return email from twam.People for a given phone (matches last 10 digits), or None."""
    try:
        digits = "".join(c for c in phone if c.isdigit())
        last10 = digits[-10:] if len(digits) >= 10 else digits
        row = db.execute(text("""
            SELECT p."EmailId" FROM twam."People" p
            WHERE  RIGHT(REGEXP_REPLACE(p."PhoneNumber", '[^0-9]', '', 'g'), 10) = :phone
              AND  p."DeletedInd" = false
              AND  p."EmailId" IS NOT NULL AND p."EmailId" != ''
            LIMIT 1
        """), {"phone": last10}).fetchone()
        return row[0] if row else None
    except Exception as e:
        print(f"[Newsletter] email lookup by phone failed: {e}")
        return None


# ── Normalisation ─────────────────────────────────────────────────────────────

def _normalise_phone(phone: str) -> Optional[str]:
    if not phone:
        return None
    digits = "".join(c for c in phone if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else None

def _strip_html(html: str) -> str:
    import re
    t = re.sub(r"<[^>]+>", "", html)
    t = re.sub(r"\n{3,}", "\n\n", t.strip())
    return t[:1000]

def _dedup(lst: List[str]) -> List[str]:
    """Remove duplicates preserving insertion order. Case-insensitive for emails."""
    seen = set()
    result = []
    for item in lst:
        key = item.lower().strip()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


# ── Welcome message templates ─────────────────────────────────────────────────

def _welcome_email_html() -> str:
    return f"""
      <h2 style="color:#6B0F2A;">Welcome to TWAM! 🎉</h2>
      <p style="color:#444;line-height:1.7;">
        You are now subscribed to TWAM updates.<br>
        Be the first to know about new arrivals, exclusive offers and comfort tips.
      </p>
      <p style="margin-top:24px;">
        <a href="{FRONTEND_BASE}/user-products/product-list"
           style="background:#6B0F2A;color:#fff;padding:12px 28px;
                  border-radius:8px;text-decoration:none;font-weight:600;">
          Shop Now
        </a>
      </p>"""

def _welcome_wa_msg() -> str:
    return (
        "Hi! You are now subscribed to *TWAM* updates!\n\n"
        "Be the first to know about new arrivals, exclusive offers & comfort tips.\n\n"
        "Shop now: " + FRONTEND_BASE + "/user-products/product-list\n\n"
        "Reply STOP to unsubscribe.\n\n"
        "- TWAM Team"
    )


# ── Send helpers with full debug logging ──────────────────────────────────────

def _send_welcome_email(db: Session, email: str, case: str) -> None:
    try:
        ok = send_email(db, EmailCreate(
            email   = email,
            subject = "Welcome to TWAM — You're subscribed!",
            message = wrap_marketing_email(email, _welcome_email_html()),
        ))
        if ok:
            print(f"[Newsletter-{case}] ✅ Welcome email sent to {email}")
        else:
            print(f"[Newsletter-{case}] ❌ Welcome email SMTP failed for {email}")
    except Exception as e:
        print(f"[Newsletter-{case}] ❌ Welcome email exception for {email}: {e}")

def _send_welcome_wa(db: Session, phone: str, case: str) -> None:
    print(f"[WA-DEBUG-{case}] Attempting WhatsApp to phone={phone!r}")
    if not phone:
        print(f"[WA-DEBUG-{case}] SKIP — phone is empty")
        return
    try:
        ok = send_whatsapp(db, phone, _welcome_wa_msg(), "NEWSLETTER_WELCOME")
        if ok:
            print(f"[WA-DEBUG-{case}] ✅ WhatsApp sent to {phone}")
        else:
            print(f"[WA-DEBUG-{case}] ❌ WhatsApp failed for {phone} — check sms.WhatsAppLog")
    except Exception as e:
        print(f"[WA-DEBUG-{case}] ❌ WhatsApp exception for {phone}: {type(e).__name__}: {e}")


# ── CRUD ──────────────────────────────────────────────────────────────────────

def check_subscribed(db: Session, identifier: str) -> bool:
    """Check by email OR phone (10-digit)."""
    identifier = identifier.strip()
    if "@" in identifier:
        row = db.query(Newsletter).filter(
            Newsletter.email      == identifier.lower(),
            Newsletter.deletedInd == False,
        ).first()
    else:
        norm = _normalise_phone(identifier)
        row = db.query(Newsletter).filter(
            Newsletter.phone      == norm,
            Newsletter.deletedInd == False,
        ).first() if norm else None
    return row is not None and row.isActive is True


def subscribe(
    db: Session,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    created_by: Optional[str] = None,
    org_id: Optional[int] = None,
) -> str:
    norm_email = email.lower().strip() if email and "@" in email else None
    norm_phone = _normalise_phone(phone) if phone else None

    if not norm_email and not norm_phone:
        return "invalid"

    print(f"[Newsletter] subscribe: email={norm_email!r} phone={norm_phone!r}")

    # ── Step 1: Resolve both channels via People lookup ───────────────────────
    resolved_email = norm_email
    resolved_phone = norm_phone

    if norm_email and not norm_phone:
        # Only email given — look up phone from People
        reg_phone = _get_registered_phone(db, norm_email)
        if reg_phone:
            print(f"[Newsletter] Case-3: registered — found phone {reg_phone} for {norm_email}")
            resolved_phone = reg_phone
        else:
            print(f"[Newsletter] Case-2: unregistered — email only")

    elif norm_phone and not norm_email:
        # Only phone given — look up email from People
        reg_email = _get_registered_email(db, norm_phone)
        if reg_email:
            print(f"[Newsletter] Case-4: registered — found email {reg_email} for {norm_phone}")
            resolved_email = reg_email
        else:
            print(f"[Newsletter] Case-1: unregistered — phone only")

    # Both given (Case 5) — use as-is, no People lookup needed

    # ── Step 2: Find existing rows for email and phone ────────────────────────
    row_by_email = None
    row_by_phone = None

    if resolved_email:
        row_by_email = db.query(Newsletter).filter(
            Newsletter.email      == resolved_email,
            Newsletter.deletedInd == False,
        ).first()

    if resolved_phone:
        row_by_phone = db.query(Newsletter).filter(
            Newsletter.phone      == resolved_phone,
            Newsletter.deletedInd == False,
        ).first()

    # ── Step 3: Merge or detect already-subscribed ────────────────────────────
    # Both point to the same row → already handled
    if row_by_email and row_by_phone and row_by_email.NewsletterId == row_by_phone.NewsletterId:
        primary = row_by_email
        orphan  = None
    elif row_by_email and row_by_phone:
        # Two separate rows exist — merge phone row INTO email row, deactivate phone row
        print(f"[Newsletter] Merging phone-row id={row_by_phone.NewsletterId} into email-row id={row_by_email.NewsletterId}")
        primary = row_by_email
        orphan  = row_by_phone
    elif row_by_email:
        primary = row_by_email
        orphan  = None
    elif row_by_phone:
        primary = row_by_phone
        orphan  = None
    else:
        primary = None
        orphan  = None

    # ── Step 4: Check already-subscribed ────────────────────────────────────
    if primary and primary.isActive and not orphan:
        # Only mark as already_subscribed if the row already has both channels
        # (or matches the channels we're trying to add). If it's missing a channel, fall through.
        already_has_email = primary.email == resolved_email if resolved_email else True
        already_has_phone = primary.phone == resolved_phone if resolved_phone else True
        if already_has_email and already_has_phone:
            print(f"[Newsletter] already_subscribed id={primary.NewsletterId}")
            return "already_subscribed"

    # ── Step 5: Save ─────────────────────────────────────────────────────────
    if primary:
        # Update existing row — add any missing channel
        primary.isActive     = True
        primary.modifiedDate = datetime.now(timezone.utc)
        primary.modifiedBy   = created_by
        if resolved_email: primary.email = resolved_email
        if resolved_phone: primary.phone = resolved_phone
        status = "resubscribed" if not primary.isActive else "updated"
        # Deactivate orphan row
        if orphan:
            orphan.isActive     = False
            orphan.deletedInd   = True
            orphan.modifiedDate = datetime.now(timezone.utc)
            orphan.modifiedBy   = "merge"
    else:
        # Brand new subscriber
        primary = Newsletter(
            email          = resolved_email,
            phone          = resolved_phone,
            isActive       = True,
            deletedInd     = False,
            createdBy      = created_by,
            organizationId = org_id,
        )
        db.add(primary)
        status = "new"

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"[Newsletter] DB commit failed: {e}")
        return "error"

    print(f"[Newsletter] Saved id={primary.NewsletterId} "
          f"email={primary.email!r} phone={primary.phone!r} status={status}")

    # ── Step 6: Send welcome messages ────────────────────────────────────────
    case_label = (
        "1" if not resolved_email and resolved_phone else
        "2" if resolved_email and not resolved_phone else
        "3" if norm_email and resolved_phone != norm_phone else
        "4" if norm_phone and resolved_email != norm_email else "5"
    )

    if resolved_email:
        _send_welcome_email(db, resolved_email, f"case-{case_label}")

    if resolved_phone:
        _send_welcome_wa(db, resolved_phone, f"case-{case_label}")

    return status


def unsubscribe(db: Session, email: str, modified_by: Optional[str] = None) -> bool:
    normalized = email.lower().strip()
    row = db.query(Newsletter).filter(
        Newsletter.email      == normalized,
        Newsletter.deletedInd == False,
    ).first()
    if not row:
        return False
    row.isActive     = False
    row.modifiedDate = datetime.now(timezone.utc)
    row.modifiedBy   = modified_by
    db.commit()
    return True


# ── Subscriber lists ──────────────────────────────────────────────────────────

def list_active_subscribers(db: Session) -> dict:
    """
    Return separate deduplicated email list and WhatsApp list.
    BUG FIX: uses .isnot(None) not != None for proper SQL NULL comparison.
    BUG FIX: deduplicates both lists before returning.
    """
    rows = db.query(Newsletter).filter(
        Newsletter.isActive   == True,
        Newsletter.deletedInd == False,
    ).all()

    raw_email_list = [r.email for r in rows if r.email]
    raw_wa_list    = [r.phone for r in rows if r.phone]

    # Deduplicate preserving order
    email_list = _dedup(raw_email_list)
    wa_list    = _dedup(raw_wa_list)

    if len(raw_email_list) != len(email_list):
        print(f"[Newsletter] Deduped email list: {len(raw_email_list)} → {len(email_list)}")
    if len(raw_wa_list) != len(wa_list):
        print(f"[Newsletter] Deduped WA list: {len(raw_wa_list)} → {len(wa_list)}")

    return {
        "total":       len(rows),
        "email_list":  email_list,
        "wa_list":     wa_list,
        "email_count": len(email_list),
        "wa_count":    len(wa_list),
    }


# ── Blast ─────────────────────────────────────────────────────────────────────

def send_blast(
    db: Session,
    subject: str,
    body_html: str,
    sent_by: Optional[str] = None,
    media_url: Optional[str] = None,
    channels: str = "both",
) -> dict:
    """
    channels = "email"     → email list only
    channels = "whatsapp"  → WA list only
    channels = "both"      → both lists independently
    Both lists are deduplicated before sending.
    """
    lists = list_active_subscribers(db)
    email_sent = email_failed = wa_sent = wa_failed = 0
    failed_emails: List[str] = []
    failed_phones: List[str] = []

    subj = subject or "TWAM Update"
    wa_message = "*" + subj + "*\n\n" + _strip_html(body_html) + "\n\n- TWAM"

    # ── Email blast ───────────────────────────────────────────────────────────
    if channels in ("email", "both"):
        print(f"[Blast] Emailing {len(lists['email_list'])} unique addresses")
        for em in lists["email_list"]:
            try:
                ok = send_email(db, EmailCreate(
                    email   = em,
                    subject = subj,
                    message = wrap_marketing_email(em, body_html),
                    name    = sent_by,
                ))
                if ok:
                    email_sent += 1
                else:
                    email_failed += 1
                    failed_emails.append(em)
            except Exception as e:
                email_failed += 1
                failed_emails.append(em)
                print(f"[Blast] Email exception for {em}: {e}")

    # ── WhatsApp blast ────────────────────────────────────────────────────────
    if channels in ("whatsapp", "both"):
        print(f"[Blast] WhatsApp to {len(lists['wa_list'])} unique numbers")
        for ph in lists["wa_list"]:
            print(f"[WA-DEBUG-BLAST] Sending to {ph}")
            try:
                ok = send_whatsapp(db, ph, wa_message, "NEWSLETTER_BLAST", media_url or "")
                if ok:
                    wa_sent += 1
                    print(f"[WA-DEBUG-BLAST] ✅ {ph}")
                else:
                    wa_failed += 1
                    failed_phones.append(ph)
                    print(f"[WA-DEBUG-BLAST] ❌ {ph} — check sms.WhatsAppLog for gateway error")
            except Exception as e:
                wa_failed += 1
                failed_phones.append(ph)
                print(f"[WA-DEBUG-BLAST] ❌ {ph} exception: {type(e).__name__}: {e}")

    print(
        f"[Newsletter Blast] channels={channels} | "
        f"Email: {email_sent} sent / {email_failed} failed | "
        f"WhatsApp: {wa_sent} sent / {wa_failed} failed"
    )
    return {
        "total":         lists["total"],
        "email_count":   lists["email_count"],
        "wa_count":      lists["wa_count"],
        "email_sent":    email_sent,
        "email_failed":  email_failed,
        "wa_sent":       wa_sent,
        "wa_failed":     wa_failed,
        "failed_emails": failed_emails,
        "failed_phones": failed_phones,
    }