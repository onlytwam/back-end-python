"""
app/whatsapp/service.py
────────────────────────
Core WhatsApp sending logic using the WAPP API.

Credentials are read from sms.WhatsAppCredential (DB), never hardcoded.

API:  http://web.cloudwhatsapp.com/wapp/api/send
Params:
    apikey  — from DB
    mobile  — 10-digit mobile number (no country code, e.g. 9392530576)
    msg     — message text

Returns JSON:  { "status": "success"|"error", "id": "...", ... }
               OR Spring-Boot error shape: { "status": 400, "error": "..." }

FIXES APPLIED:
  1. _normalise_phone() returns 10-digit number only (no "91" prefix).
     The WAPP API (web.cloudwhatsapp.com) example shows 10-digit numbers —
     sending 12 digits (91XXXXXXXXXX) caused HTTP 400 / ERROR responses.
  2. param key kept as "mobile" — matches the working example URL in the
     API docs (&mobile=989898XXXX). The docs table said "number" but the
     live example uses "mobile"; reverted after confirming 400 with "number".
  3. ok check uses str() wrap before .lower() — the API can return
     "status" as an integer (e.g. 400) in error responses, which crashed
     the previous code with 'int' object has no attribute 'lower'.
  4. Removed `or response.status_code == 200` from ok — that silently
     marked failed sends as SENT in the log.
"""

import time
try:
    import httpx
except ImportError:
    httpx = None  # type: ignore
from typing import Optional
from sqlalchemy.orm import Session
from .models import WhatsAppCredential, WhatsAppLog


# ── Credential cache (5-minute TTL) ──────────────────────────────────────────

_cred_cache: Optional[dict] = None
_cred_ts:    float          = 0.0
_CRED_TTL                   = 300   # seconds


def _get_credential(db: Session) -> Optional[dict]:
    """
    Return active WhatsApp credential from sms.WhatsAppCredential.
    Cached for 5 minutes. Returns None if no active row exists.
    """
    global _cred_cache, _cred_ts
    if time.time() - _cred_ts < _CRED_TTL and _cred_cache is not None:
        return _cred_cache

    try:
        row = db.query(WhatsAppCredential).filter(
            WhatsAppCredential.isActive   == True,
            WhatsAppCredential.deletedInd == False,
        ).first()

        if row:
            _cred_cache = {
                "apiUrl":  row.apiUrl.rstrip("?"),   # strip trailing ? — we build params ourselves
                "apiKey":  row.apiKey,
                "timeout": row.timeout or 10,
            }
        else:
            _cred_cache = {}

        _cred_ts = time.time()
    except Exception as e:
        print(f"[WhatsApp] Could not load WhatsAppCredential: {e}")
        _cred_cache = {}
        _cred_ts = time.time()

    return _cred_cache if _cred_cache else None


# ── Log helper ────────────────────────────────────────────────────────────────

def _log(
    db: Session,
    recipient: str,
    message_type: str,
    message_content: str,
    media_url: str,
    transaction_id: str,
    state: str,
    status_code: int,
    error_desc: str = "",
) -> None:
    """Write one row to sms.WhatsAppLog. Never raises."""
    try:
        db.add(WhatsAppLog(
            recipient      = recipient,
            messageType    = message_type,
            messageContent = message_content,
            mediaUrl       = media_url or None,
            transactionId  = transaction_id,
            state          = state,
            statusCode     = status_code,
            errorDesc      = error_desc or None,
        ))
        db.commit()
    except Exception as e:
        print(f"[WhatsApp Log] Failed to write: {e}")


# ── Phone normalisation ───────────────────────────────────────────────────────

def _normalise_phone(phone: str) -> Optional[str]:
    """
    Strip non-digits and return the last 10 digits (Indian mobile number).
    The WAPP API (web.cloudwhatsapp.com) expects a 10-digit number WITHOUT
    the country code prefix. Sending 12 digits (91XXXXXXXXXX) causes HTTP 400.
    Returns None if fewer than 10 digits are found.

    FIX: removed "91" prefix — was sending 12 digits, API wants 10.
    """
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) < 10:
        return None
    return digits[-10:]   # 10 digits only, no "91" prefix


# ── Core send ─────────────────────────────────────────────────────────────────

def send_whatsapp(
    db: Session,
    phone: str,
    message: str,
    message_type: str = "GENERAL",
    media_url: str = "",
) -> bool:
    """
    Send a WhatsApp message via the WAPP API.
    Credentials are read from sms.WhatsAppCredential (not hardcoded).

    Args:
        db           — SQLAlchemy session (for credential lookup + logging)
        phone        — recipient phone (10 or 12 digits, with or without +91)
        message      — plain text message body
        message_type — label for the log (e.g. ORDER_PLACED, NEWSLETTER)
        media_url    — optional publicly accessible image URL

    Returns True on success, False on failure.
    """
    cred = _get_credential(db)
    if not cred:
        print(f"[WhatsApp] No active WhatsAppCredential in DB — skipping {message_type}")
        return False

    recipient = _normalise_phone(phone)
    if not recipient:
        print(f"[WhatsApp] Invalid phone: {phone!r}")
        return False

    # "mobile" matches the working example URL in the API docs:
    # ...&mobile=989898XXXX&msg=testmsg
    params: dict = {
        "apikey": cred["apiKey"],
        "mobile": recipient,          # 10-digit number, no country code
        "msg":    message,
    }
    if media_url:
        params["mediaUrl"] = media_url

    try:
        response = httpx.get(cred["apiUrl"], params=params, timeout=cred["timeout"])
        print(f"[WhatsApp] HTTP {response.status_code} → {response.text}")

        try:
            data = response.json()
        except Exception:
            data = {}

        # FIX: str() wrap on status — the API returns "status" as an integer
        # (e.g. 400) in Spring-Boot error responses, which crashed .lower() before.
        # FIX: removed `or response.status_code == 200` — that caused every
        # HTTP-200 error response to be silently logged as SENT.
        ok          = str(data.get("status", "")).lower() == "success"
        txn_id      = str(data.get("id", "") or data.get("msgId", ""))
        state_str   = "SENT" if ok else "FAILED"
        status_code = response.status_code
        error_desc  = (
            "" if ok
            else (data.get("message", "") or data.get("error", "") or response.text)
        )

        _log(db, recipient, message_type, message, media_url,
             txn_id, state_str, status_code, error_desc)

        if ok:
            print(f"[WhatsApp] ✅ Sent {message_type} to {recipient}, txId={txn_id}")
        else:
            print(f"[WhatsApp] ❌ Failed {message_type} to {recipient}: {error_desc}")
        return ok

    except Exception as e:
        _log(db, recipient, message_type, message, media_url,
             "", "EXCEPTION", 0, str(e))
        print(f"[WhatsApp] Error sending to {recipient}: {e}")
        return False


def send_whatsapp_bulk(
    db: Session,
    phones: list[str],
    message: str,
    message_type: str = "GENERAL",
    media_url: str = "",
) -> dict:
    """
    Send the same WhatsApp message to multiple phone numbers.
    Returns { total, sent, failed }.
    """
    sent = failed = 0
    for phone in phones:
        ok = send_whatsapp(db, phone, message, message_type, media_url)
        if ok:
            sent += 1
        else:
            failed += 1
    return {"total": len(phones), "sent": sent, "failed": failed}