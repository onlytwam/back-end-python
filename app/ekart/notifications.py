"""
app/ekart/notifications.py
───────────────────────────
SMS and email notifications for all order lifecycle events.

SMS credential source:  sms.SmsCredential
SMS template source:    sms.SmsMessageTemplate
SMS audit log:          sms.SmsLog

DB TEMPLATE PLACEHOLDERS (named style) — matched to actual sms."SmsMessageTemplate" rows
──────────────────────────────────────────────────────────────────────────────────────────
DB MessageType  | Named placeholders in MessageContent
────────────────|────────────────────────────────────────────────────────────────────────
OTP             | {otp}
WELCOME         | (no placeholders)
ORDER_PLACED    | {customer_name}, {order_number}, {items}, {total_amount}, {delivery_address}
ORDER_PENDING   | {customer_name}, {order_number}, {total_amount}
PAYMENT_FAILURE | {customer_name}, {order_number}, {total_amount}
REFUND_AMOUNT   | {refund_amount}, {order_number}, {refund_date}
REFUND_SUCCESS  | {customer_name}, {order_number}, {refund_amount}, {payment_method}, {refund_date}
TEMP_PASSWORD   | {customer_name}, {temp_password}

SmartPing DLT API — HOW VARIABLES ARE SENT
──────────────────────────────────────────
SmartPing uses POSITIONAL vars: var1, var2, var3, … as query params.
The order of positional vars must match the order placeholders appear
in the DLT-registered template (left-to-right).

DO NOT send a "text" param — causes Error 5307 (template mismatch).
SmartPing fetches the template by dltContentId and substitutes on their side.

POSITIONAL ORDER per template (var1=first placeholder, var2=second, …):
────────────────────────────────────────────────────────────────────────
ORDER_PLACED    | var1=customer_name, var2=order_number, var3=items, var4=total_amount, var5=delivery_address
ORDER_PENDING   | var1=customer_name, var2=order_number, var3=total_amount
PAYMENT_FAILURE | var1=customer_name, var2=order_number, var3=total_amount
REFUND_AMOUNT   | var1=refund_amount, var2=order_number, var3=refund_date
REFUND_SUCCESS  | var1=customer_name, var2=order_number, var3=refund_amount, var4=payment_method, var5=refund_date
TEMP_PASSWORD   | var1=customer_name, var2=temp_password
OTP             | var1=otp
WELCOME         | (no vars)

SmartPing HTTP 400 — TROUBLESHOOTING
─────────────────────────────────────
HTTP 400 with empty body usually means one of:
  1. Wrong HTTP method  → code tries GET first, then falls back to POST automatically
  2. Wrong DLT param names → code sends both alias sets (dltContentId + dltTemplateId, etc.)
  3. Bad credentials / senderId → check sms.SmsCredential rows
  4. Phone number format wrong → must be 12-digit 91XXXXXXXXXX
Enable debug logging by setting SMS_DEBUG=true in environment or
SmsCredential."Debug"=true in DB.  Full URL + params are printed.
"""

import os
import time
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode

from sqlalchemy.orm import Session
from sqlalchemy import Enum, text

from app.sendemail.repository import send_email
from app.sendemail.schemas import EmailCreate


# ── AppSettings cache (5-minute TTL) ─────────────────────────────────────────
_settings_cache: dict = {}
_settings_ts: float = 0.0
_SETTINGS_TTL = 300


def _get_app_settings(db: Session) -> dict:
    global _settings_cache, _settings_ts
    if time.time() - _settings_ts < _SETTINGS_TTL and _settings_cache:
        return _settings_cache
    try:
        rows = db.execute(
            text('SELECT "Key","Value" FROM twam."AppSettings" WHERE "DeletedInd"=false')
        ).fetchall()
        _settings_cache = {r[0]: r[1] for r in rows}
        _settings_ts = time.time()
    except Exception as e:
        print(f"[Notifications] Could not load AppSettings: {e}")
        _settings_cache = {}
        _settings_ts = time.time()
    return _settings_cache


def _cfg(db: Session, key: str, fallback: str = "") -> str:
    return _get_app_settings(db).get(key) or fallback


# ── SmsCredential cache (5-minute TTL) ───────────────────────────────────────
_cred_cache = "UNSET"
_cred_ts: float = 0.0
_CRED_TTL = 300


def _get_sms_credential(db: Session) -> Optional[dict]:
    global _cred_cache, _cred_ts
    if _cred_cache != "UNSET" and time.time() - _cred_ts < _CRED_TTL:
        return _cred_cache

    try:
        row = db.execute(text("""
            SELECT "ApiUrl", "ApiMultiUrl", "Username", "Password",
                   "SenderId", "Unicode"
            FROM sms."SmsCredential"
            WHERE "IsActive" = true AND "DeletedInd" = false
            LIMIT 1
        """)).fetchone()

        if row:
            _cred_cache = {
                "apiUrl":      row[0] or "",
                "apiMultiUrl": row[1] or row[0] or "",
                "username":    row[2] or "",
                "password":    row[3] or "",
                "senderId":    row[4] or "TWAMCH",
                "unicode":     row[5] or "false",
            }
        else:
            print("[SMS] No active SmsCredential row found in sms.SmsCredential")
            _cred_cache = None

        _cred_ts = time.time()

    except Exception as e:
        print(f"[SMS] Could not load SmsCredential: {e}")
        _cred_cache = None
        _cred_ts = time.time()

    return _cred_cache


# ── SmsMessageTemplate cache (5-minute TTL) ──────────────────────────────────
_template_cache: dict = {}
_template_ts: float = 0.0
_template_cache_loaded: bool = False
_TEMPLATE_TTL = 300


def _get_all_templates(db: Session) -> dict:
    global _template_cache, _template_ts, _template_cache_loaded
    if _template_cache_loaded and time.time() - _template_ts < _TEMPLATE_TTL:
        return _template_cache

    try:
        rows = db.execute(text("""
            SELECT "MessageType", "MessageContent", "DLTId", "TemplateId"
            FROM sms."SmsMessageTemplate"
            WHERE "DeletedInd" = false
              AND LOWER("State") = 'active'
        """)).fetchall()

        _template_cache = {}
        for row in rows:
            msg_type, content, dlt_peid, tpl_id = row
            if msg_type:
                _template_cache[msg_type] = {
                    "messageContent": content  or "",
                    "dltId":          dlt_peid or "",
                    "templateId":     tpl_id   or "",
                }

        _template_cache_loaded = True
        _template_ts = time.time()
        print(f"[SMS] Loaded {len(_template_cache)} active templates: {list(_template_cache.keys())}")

    except Exception as e:
        print(f"[SMS] Could not load SmsMessageTemplate: {e}")
        _template_cache = {}
        _template_cache_loaded = True
        _template_ts = time.time()

    return _template_cache


def _get_template(db: Session, message_type: str) -> Optional[dict]:
    return _get_all_templates(db).get(message_type)


def _render_named_template(content: str, named_vars: dict) -> str:
    """
    Substitute DB named placeholders {customer_name}, {order_number}, …
    Used ONLY for SmsLog.MessageContent rendering — never sent to SmartPing.
    """
    for key, value in named_vars.items():
        content = content.replace("{" + key + "}", str(value) if value is not None else "")
    return content


# ── MessageType constants ─────────────────────────────────────────────────────
MT_ORDER_PLACED       = "ORDER_PLACED"
MT_ORDER_SHIPPED      = "ORDER_SHIPPED"
MT_OUT_FOR_DELIVERY   = "ORDER_OUT_FOR_DELIVERY"
MT_ORDER_DELIVERED    = "ORDER_DELIVERED"
MT_ORDER_CANCELLED    = "ORDER_CANCELLED"
MT_RETURN_INITIATED   = "RETURN_INITIATED"
MT_ORDER_PENDING      = "ORDER_PENDING"
MT_PAYMENT_FAILURE    = "PAYMENT_FAILURE"
MT_REFUND_AMOUNT      = "REFUND_AMOUNT"
MT_REFUND_SUCCESS     = "REFUND_SUCCESS"
MT_WELCOME            = "WELCOME"
MT_OTP                = "OTP"
MT_TEMPORARY_PASSWORD = "TEMP_PASSWORD"


# ── Order helpers ─────────────────────────────────────────────────────────────

def _get_order_details(db: Session, order_id: int) -> Optional[dict]:
    row = db.execute(text("""
        SELECT
            o."OrderId", o."OrderNumber", o."TotalAmount", o."State",
            o."TrackingId", o."IsWhatsappNotification", o."UserProfileId",
            o."DeliveredDate", o."DeliveryCharge",
            a."Name"        AS customer_name,
            a."Phone"       AS customer_phone,
            a."AddressLine" AS address_line,
            a."City"        AS city,
            a."PinCode"     AS pincode,
            p."EmailId"     AS customer_email
        FROM twam."Orders" o
        LEFT JOIN twam."Address" a ON a."AddressId" = o."ShippingAddressId"
        LEFT JOIN twam."People"  p ON p."UserProfileId" = o."UserProfileId"
        WHERE o."OrderId" = :order_id AND o."DeletedInd" = false
        LIMIT 1
    """), {"order_id": order_id}).fetchone()
    if not row:
        return None
    return {
        "order_id":          row[0],
        "order_number":      row[1] or str(row[0]),
        "total_amount":      float(row[2]) if row[2] else 0,
        "state":             row[3],
        "tracking_id":       row[4],
        "whatsapp_opted_in": row[5] or False,
        "user_profile_id":   row[6],
        "delivered_date":    row[7],
        "delivery_charge":   float(row[8]) if row[8] else 0,
        "customer_name":     row[9]  or "Valued Customer",
        "customer_phone":    row[10],
        "address_line":      row[11],
        "city":              row[12],
        "pincode":           row[13],
        "customer_email":    row[14],
    }


def _get_order_items(db: Session, order_id: int) -> list:
    rows = db.execute(text("""
        SELECT p."Name", oi."Quantity", oi."Price", pv."Color", s."SizeLabel"
        FROM twam."OrderItems" oi
        LEFT JOIN twam."Products" p ON p."ProductId" = oi."ProductId"
        LEFT JOIN twam."ProductVariantDetail" pvd
               ON pvd."ProductVariantDetailId" = oi."ProductVariantDetailId"
        LEFT JOIN twam."ProductVariants" pv ON pv."ProductVariantId" = pvd."ProductVariantId"
        LEFT JOIN mdm."Size" s ON s."SizeId" = pvd."Size"
        WHERE oi."OrderId" = :order_id AND oi."DeletedInd" = false
    """), {"order_id": order_id}).fetchall()
    return [
        {"name": r[0] or "Product", "quantity": r[1] or 1,
         "price": float(r[2]) if r[2] else 0, "color": r[3], "size": r[4]}
        for r in rows
    ]


def _fmt(amount: float) -> str:
    return f"Rs.{amount:,.2f}"


def _fmt_items_sms(items: list) -> str:
    return ", ".join(f"{i['name']} x{i['quantity']}" for i in items) or "Items"


# ── Email HTML helpers ────────────────────────────────────────────────────────

def _header(company_name: str) -> str:
    return f"""
    <div style="background:#f8f5f0;padding:20px;text-align:center;
                border-bottom:3px solid #8b7355;">
      <h1 style="margin:0;color:#8b7355;font-family:Georgia,serif;">{company_name}</h1>
    </div>"""


def _footer(company_name: str, support_email: str) -> str:
    return f"""
    <div style="background:#f8f5f0;padding:20px;text-align:center;
                margin-top:30px;border-top:1px solid #e5e0d8;">
      <p style="margin:0 0 10px;color:#666;font-size:14px;">
        Questions? <a href="mailto:{support_email}"
                      style="color:#8b7355;">{support_email}</a>
      </p>
      <p style="margin:0;color:#999;font-size:12px;">
        © {datetime.now().year} {company_name}. All rights reserved.
      </p>
    </div>"""


def _items_table(items: list) -> str:
    rows_html = ""
    for item in items:
        details = []
        if item.get("size"):  details.append(f"Size: {item['size']}")
        if item.get("color"): details.append(f"Color: {item['color']}")
        det = (f"<br><small style='color:#666'>{' | '.join(details)}</small>"
               if details else "")
        rows_html += f"""
        <tr>
          <td style="padding:12px;border-bottom:1px solid #eee;">{item['name']}{det}</td>
          <td style="padding:12px;border-bottom:1px solid #eee;text-align:center;">{item['quantity']}</td>
          <td style="padding:12px;border-bottom:1px solid #eee;text-align:right;">{_fmt(item['price'])}</td>
        </tr>"""
    return f"""
    <table style="width:100%;border-collapse:collapse;margin:20px 0;">
      <thead><tr style="background:#f8f5f0;">
        <th style="padding:12px;text-align:left;border-bottom:2px solid #8b7355;">Item</th>
        <th style="padding:12px;text-align:center;border-bottom:2px solid #8b7355;">Qty</th>
        <th style="padding:12px;text-align:right;border-bottom:2px solid #8b7355;">Price</th>
      </tr></thead>
      <tbody>{rows_html}</tbody>
    </table>"""


# ── SmsLog writer ─────────────────────────────────────────────────────────────

def _log_sms(
    db: Session,
    recipient: str,
    message_type: str,
    message_content: str,
    template_id: str,
    dlt_peid: str,
    transaction_id: str,
    state: str,
    status_code: int,
    error_desc: str = "",
) -> None:
    try:
        db.execute(text("""
            INSERT INTO sms."SmsLog" (
                "Recipient", "MessageType", "MessageContent",
                "TemplateId", "DltPrincipalEntityId", "TransactionId",
                "State", "StatusCode", "ErrorDesc", "SentAt", "DeletedInd"
            ) VALUES (
                :recipient, :message_type, :message_content,
                :template_id, :dlt_peid, :transaction_id,
                :state, :status_code, :error_desc, NOW(), false
            )
        """), {
            "recipient":       recipient,
            "message_type":    message_type,
            "message_content": message_content,
            "template_id":     template_id,
            "dlt_peid":        dlt_peid,
            "transaction_id":  transaction_id,
            "state":           state,
            "status_code":     status_code,
            "error_desc":      error_desc,
        })
        db.commit()
    except Exception as e:
        print(f"[SMS Log] Failed to write SmsLog: {e}")
        try:
            db.rollback()
        except Exception:
            pass


# ── SmartPing request helper ──────────────────────────────────────────────────

def _smartping_request(api_url: str, params: dict):

    import httpx

    try:

        print(f"[SMS DEBUG] URL  : {api_url}")
        print(f"[SMS DEBUG] PARAMS: {params}")

        resp = httpx.post(
            api_url,
            data=params,
            timeout=15
        )

        raw = resp.text.strip()

        print(f"[SMS DEBUG] STATUS: {resp.status_code}")
        print(f"[SMS DEBUG] BODY  : {raw}")

        return resp.status_code, raw, {
            "statusCode": resp.status_code,
            "state": "SUCCESS" if resp.status_code == 200 else "ERROR",
            "description": raw,
        }

    except Exception as e:

        print(f"[SMS ERROR] {e}")

        return 0, str(e), {
            "statusCode": 0,
            "state": "EXCEPTION",
            "description": str(e),
        }


# ── SMS sender ────────────────────────────────────────────────────────────────

def _send_sms(
    db: Session,
    phone: str,
    message_type: str,
    named_vars: dict,
    positional_order: list,
) -> bool:
    """
    Send SMS via SmartPing HTTP API.

    named_vars       : dict matching the DB template's {placeholder} names.
                       Used for SmsLog rendering and building var1, var2, … params.
    positional_order : keys from named_vars in the order SmartPing expects them.
                       e.g. ["customer_name", "order_number", "total_amount"]
                       → var1=named_vars["customer_name"], var2=…

    CRITICAL: DO NOT send "text" param → causes Error 5307.

    HTTP 400 handling
    ─────────────────
    If GET returns 400 with empty body, the call is automatically retried
    as POST. Both alias sets for DLT param names are always sent.
    Set env SMS_DEBUG=true to log full URL + params for troubleshooting.
    """
    # ── Guard: credential ────────────────────────────────────────────────────
    cred = _get_sms_credential(db)
    if not cred:
        print(f"[SMS] No active SmsCredential — skipping {message_type}")
        return False
    if not cred.get("apiUrl") or not cred.get("username"):
        print(f"[SMS] Incomplete SmsCredential — skipping {message_type}")
        return False

    # ── Guard: template ──────────────────────────────────────────────────────
    template = _get_template(db, message_type)
    if not template:
        print(f"[SMS] No active template for '{message_type}' in sms.SmsMessageTemplate")
        return False

    dlt_content_id  = template["templateId"]
    dlt_peid        = template["dltId"]
    message_content = template["messageContent"]

    if not dlt_content_id:
        print(f"[SMS] Template '{message_type}' missing TemplateId — cannot send")
        return False

    # ── Normalise phone → 12-digit (91XXXXXXXXXX) ───────────────────────────
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if len(digits) < 10:
        print(f"[SMS] Invalid phone: '{phone}'")
        return False
    recipient = "91" + digits[-10:]

    log_text = _render_named_template(message_content, named_vars)

    print(f"[SMS] Sending {message_type} → {recipient}")
    print(f"[SMS] dltContentId={dlt_content_id} | dltPEID={dlt_peid}")
    for i, key in enumerate(positional_order, start=1):
        print(f"[SMS] var{i} ({key}) = {named_vars.get(key, '')}")

    try:
        # ── Build SmartPing params ───────────────────────────────────────────
        # params: dict = {
        #     "username":             cred["username"],
        #     "password":             cred["password"],
        #     "from":                 cred["senderId"],
        #     "to":                   recipient,
        #     "dltContentId":         dlt_content_id,
        #     "dltPrincipalEntityId": dlt_peid,
        # }
        params: dict = {
            "username": cred["username"],
            "password": cred["password"],
            "from": cred["senderId"],
            "to": recipient,
            "dltContentId": dlt_content_id,
            "dltPrincipalEntityId": dlt_peid,
            "unicode": "true" if cred.get("unicode", "").lower() == "true" else "false",
        }

        # Positional vars: var1, var2, …
        for i, key in enumerate(positional_order, start=1):
            params[f"var{i}"] = str(named_vars.get(key, "") or "")

        # ── Fire request ─────────────────────────────────────────────────────────
        http_status, raw, data = _smartping_request(cred["apiUrl"], params)  # ← apiUrl not apiMultiUrl

        print(f"[SMS] HTTP {http_status} | {raw[:300]}")

        ok          = data.get("statusCode") == 200 or data.get("state") == "SUBMIT_ACCEPTED"
        txn_id      = str(data.get("transactionId", ""))
        state_str   = data.get("state", "UNKNOWN")
        status_code = int(data.get("statusCode") or http_status or 0)
        error_desc  = "" if ok else (data.get("description") or raw[:200])

        _log_sms(db, recipient, message_type, log_text,
                 dlt_content_id, dlt_peid, txn_id, state_str, status_code, error_desc)

        if ok:
            print(f"[SMS] ✅ {message_type} → {recipient} txId={txn_id}")
        else:
            print(
                f"[SMS] ❌ {message_type} FAILED "
                f"code={status_code} state={state_str} desc={error_desc}\n"
                f"[SMS] ── TROUBLESHOOT ──────────────────────────────────\n"
                f"[SMS]   Set SMS_DEBUG=true in env to log full URL + params\n"
                f"[SMS]   Verify sms.SmsCredential: username, password, senderId, apiUrl\n"
                f"[SMS]   Verify sms.SmsMessageTemplate: TemplateId (DLT Content ID), DLTId (PE ID)\n"
                f"[SMS] ─────────────────────────────────────────────────"
            )

        return ok

    except Exception as e:
        _log_sms(db, recipient, message_type, log_text,
                 dlt_content_id, dlt_peid, "", "EXCEPTION", 0, str(e))
        print(f"[SMS] EXCEPTION {message_type} → {phone}: {e}")
        return False


# ── WhatsApp ──────────────────────────────────────────────────────────────────

def _send_whatsapp(db: Session, phone: str, message: str,
                   message_type: str = "ORDER", media_url: str = "") -> bool:
    if not phone:
        return False
    try:
        from app.whatsapp.service import send_whatsapp
        return send_whatsapp(db, phone, message, message_type, media_url)
    except Exception as e:
        print(f"[WhatsApp] Failed: {e}")
        return False


# ── Notification router ───────────────────────────────────────────────────────

def _notify(
    db: Session,
    order: dict,
    message_type: str,
    named_vars: dict,
    positional_order: list,
    whatsapp_message: str,
    media_url: str = "",
) -> None:
    phone = order.get("customer_phone") or ""
    if not phone:
        return
    if order.get("whatsapp_opted_in"):
        _send_whatsapp(db, phone, whatsapp_message, message_type, media_url)
    else:
        _send_sms(db, phone, message_type, named_vars, positional_order)


# ═══════════════════════════════════════════════════════════════════════════════
# OTP
# DB template: "Only TWAM Login OTP: {otp} Valid for 10 mins…"
# Placeholders: {otp}
# Positional:   var1=otp
# ═══════════════════════════════════════════════════════════════════════════════
def send_otp_sms(db: Session, phone: str, otp: str) -> bool:
    if not phone:
        return False
    return _send_sms(
        db, phone, MT_OTP,
        named_vars={"otp": otp},
        positional_order=["otp"],
    )


# ═══════════════════════════════════════════════════════════════════════════════
# WELCOME
# DB template: "Welcome to OnlyTWAM… Visit https://onlytwam.com/…"
# Placeholders: (none)
# Positional:   (no vars)
# ═══════════════════════════════════════════════════════════════════════════════
def send_welcome_sms(db: Session, phone: str) -> bool:
    if not phone:
        return False
    return _send_sms(
        db, phone, MT_WELCOME,
        named_vars={},
        positional_order=[],
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TEMP_PASSWORD
# DB template: "Hi {customer_name} Your Temporary Password is : {temp_password}…"
# Placeholders: {customer_name}, {temp_password}
# Positional:   var1=customer_name, var2=temp_password
# ═══════════════════════════════════════════════════════════════════════════════
def send_temporary_password_sms(db: Session, phone: str, name: str, password: str) -> bool:
    if not phone:
        return False
    return _send_sms(
        db, phone, MT_TEMPORARY_PASSWORD,
        named_vars={
            "customer_name": name,
            "temp_password": password,
        },
        positional_order=["customer_name", "temp_password"],
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ORDER_PLACED
# ═══════════════════════════════════════════════════════════════════════════════
def send_order_placed_notification(db: Session, order_id: int) -> bool:
    order = _get_order_details(db, order_id)
    if not order:
        return False

    company = _cfg(db, "COMPANY_NAME", "Only TWAM")
    support = _cfg(db, "SUPPORT_EMAIL", "support@onlytwam.com")
    items   = _get_order_items(db, order_id)

    if order["customer_email"]:
        subtotal    = sum(i["price"] * i["quantity"] for i in items)
        ship_charge = _fmt(order["delivery_charge"]) if order["delivery_charge"] else "FREE"
        body = f"""
        <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#fff;">
          {_header(company)}
          <div style="padding:30px;">
            <h2 style="color:#333;margin-bottom:5px;">Thank you for your order!</h2>
            <p style="color:#666;margin-top:0;">Hi {order['customer_name']}, your order has been confirmed.</p>
            <div style="background:#f8f5f0;padding:15px;border-radius:8px;margin:20px 0;">
              <p style="margin:0;"><strong>Order Number:</strong> #{order['order_number']}</p>
              <p style="margin:5px 0 0;"><strong>Order Date:</strong> {datetime.now().strftime('%d %B %Y')}</p>
            </div>
            {_items_table(items)}
            <div style="text-align:right;padding:15px 0;border-top:2px solid #8b7355;">
              <p style="margin:5px 0;"><span style="color:#666;">Subtotal:</span> <strong>{_fmt(subtotal)}</strong></p>
              <p style="margin:5px 0;"><span style="color:#666;">Shipping:</span> <strong>{ship_charge}</strong></p>
              <p style="margin:10px 0 0;font-size:18px;"><span style="color:#666;">Total:</span>
                <strong style="color:#8b7355;">{_fmt(order['total_amount'])}</strong></p>
            </div>
            <div style="background:#fff8e7;padding:15px;border-radius:8px;border-left:4px solid #f5a623;margin:20px 0;">
              <p style="margin:0;color:#333;">We'll send tracking details once your order ships!</p>
            </div>
            <h3 style="color:#8b7355;">Delivery Address</h3>
            <p style="color:#666;line-height:1.6;">
              {order['customer_name']}<br>{order['address_line'] or ''}<br>
              {order['city'] or ''} - {order['pincode'] or ''}<br>{order['customer_phone'] or ''}
            </p>
          </div>
          {_footer(company, support)}
        </div>"""
        send_email(db, EmailCreate(
            name=order["customer_name"], email=order["customer_email"],
            subject=f"Order Confirmed! #{order['order_number']} - {company}", message=body,
        ))

    if order["customer_phone"]:
        delivery_address = " ".join(filter(None, [
            order["address_line"], order["city"], order["pincode"]
        ]))
        _notify(
            db, order, MT_ORDER_PLACED,
            named_vars={
                "customer_name":    order["customer_name"],
                "order_number":     order["order_number"],
                "items":            _fmt_items_sms(items),
                "total_amount":     _fmt(order["total_amount"]),
                "delivery_address": delivery_address,
            },
            positional_order=[
                "customer_name", "order_number", "items",
                "total_amount", "delivery_address",
            ],
            whatsapp_message=(
                f"Order Confirmed!\n\n"
                f"Hi {order['customer_name']}, your order #{order['order_number']} has been placed.\n"
                f"Items: {_fmt_items_sms(items)}\n"
                f"Amount: {_fmt(order['total_amount'])}\n"
                f"Delivery to: {delivery_address}\n\n"
                f"We'll notify you when it ships! — TWAM"
            ),
        )
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# ORDER_PENDING
# ═══════════════════════════════════════════════════════════════════════════════
def send_order_pending_notification(db: Session, order_id: int) -> bool:
    order = _get_order_details(db, order_id)
    if not order:
        return False
    company = _cfg(db, "COMPANY_NAME", "Only TWAM")
    support = _cfg(db, "SUPPORT_EMAIL", "support@onlytwam.com")

    if order["customer_email"]:
        body = f"""
        <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#fff;">
          {_header(company)}
          <div style="padding:30px;">
            <h2 style="color:#f5a623;">Payment Pending</h2>
            <p style="color:#666;">Hi {order['customer_name']},</p>
            <p style="color:#666;">Order <strong>#{order['order_number']}</strong> received but payment confirmation is pending.</p>
            <div style="background:#fff8e7;padding:15px;border-radius:8px;border-left:4px solid #f5a623;margin:20px 0;">
              <p style="margin:0;"><strong>Order:</strong> #{order['order_number']}</p>
              <p style="margin:5px 0 0;"><strong>Amount:</strong> {_fmt(order['total_amount'])}</p>
            </div>
            <p style="color:#666;">We'll notify you once payment is confirmed.
              Questions? <a href="mailto:{support}" style="color:#8b7355;">{support}</a></p>
          </div>
          {_footer(company, support)}
        </div>"""
        send_email(db, EmailCreate(
            name=order["customer_name"], email=order["customer_email"],
            subject=f"Order Payment Pending - #{order['order_number']}", message=body,
        ))

    if order["customer_phone"]:
        _notify(
            db, order, MT_ORDER_PENDING,
            named_vars={
                "customer_name": order["customer_name"],
                "order_number":  order["order_number"],
                "total_amount":  _fmt(order["total_amount"]),
            },
            positional_order=["customer_name", "order_number", "total_amount"],
            whatsapp_message=(
                f"Payment Pending\n\n"
                f"Hi {order['customer_name']}, your order #{order['order_number']} is pending payment.\n"
                f"Amount: {_fmt(order['total_amount'])}\n\nWe'll update you once confirmed. — TWAM"
            ),
        )
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# PAYMENT_FAILURE
# ═══════════════════════════════════════════════════════════════════════════════
def send_payment_failure_notification(
    db: Session, order_id: int, total_amount: Optional[float] = None
) -> bool:
    order = _get_order_details(db, order_id)
    if not order:
        return False
    company = _cfg(db, "COMPANY_NAME", "Only TWAM")
    support = _cfg(db, "SUPPORT_EMAIL", "support@onlytwam.com")
    amount  = total_amount or order["total_amount"]

    if order["customer_email"]:
        body = f"""
        <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#fff;">
          {_header(company)}
          <div style="padding:30px;">
            <h2 style="color:#c0392b;">Payment Failed</h2>
            <p style="color:#666;">Hi {order['customer_name']},</p>
            <p style="color:#666;">Payment for order <strong>#{order['order_number']}</strong> could not be processed.</p>
            <div style="background:#fdf2f2;padding:15px;border-radius:8px;border-left:4px solid #c0392b;margin:20px 0;">
              <p style="margin:0;"><strong>Order:</strong> #{order['order_number']}</p>
              <p style="margin:5px 0 0;"><strong>Amount:</strong> {_fmt(amount)}</p>
            </div>
            <p style="color:#666;">Please retry with a different payment method.
              If the amount was deducted it will be refunded within 3-7 business days.</p>
            <p style="color:#666;">Need help? <a href="mailto:{support}" style="color:#8b7355;">{support}</a></p>
          </div>
          {_footer(company, support)}
        </div>"""
        send_email(db, EmailCreate(
            name=order["customer_name"], email=order["customer_email"],
            subject=f"Payment Failed - Order #{order['order_number']}", message=body,
        ))

    if order["customer_phone"]:
        _notify(
            db, order, MT_PAYMENT_FAILURE,
            named_vars={
                "customer_name": order["customer_name"],
                "order_number":  order["order_number"],
                "total_amount":  _fmt(amount),
            },
            positional_order=["customer_name", "order_number", "total_amount"],
            whatsapp_message=(
                f"Payment Failed\n\n"
                f"Hi {order['customer_name']}, payment for #{order['order_number']} failed.\n"
                f"Amount: {_fmt(amount)}\n"
                f"Please retry or use a different method.\nRefund if deducted: 3-7 days. — TWAM"
            ),
        )
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# REFUND_AMOUNT
# ═══════════════════════════════════════════════════════════════════════════════
def send_refund_amount_notification(
    db: Session, order_id: int, refund_amount: float, refund_date: Optional[str] = None
) -> bool:
    order = _get_order_details(db, order_id)
    if not order or not order["customer_phone"]:
        return False
    fmt_amount = _fmt(refund_amount)
    fmt_date   = refund_date or datetime.now().strftime("%d %b %Y")
    return _send_sms(
        db, order["customer_phone"], MT_REFUND_AMOUNT,
        named_vars={
            "refund_amount": fmt_amount,
            "order_number":  order["order_number"],
            "refund_date":   fmt_date,
        },
        positional_order=["refund_amount", "order_number", "refund_date"],
    )


# ═══════════════════════════════════════════════════════════════════════════════
# REFUND_SUCCESS
# ═══════════════════════════════════════════════════════════════════════════════
def send_refund_success_notification(
    db: Session,
    order_id: int,
    refund_amount: float,
    payment_method: str = "Original Payment Method",
    refund_date: Optional[str] = None,
) -> bool:
    order = _get_order_details(db, order_id)
    if not order:
        return False
    company    = _cfg(db, "COMPANY_NAME", "Only TWAM")
    support    = _cfg(db, "SUPPORT_EMAIL", "support@onlytwam.com")
    fmt_amount = _fmt(refund_amount)
    fmt_date   = refund_date or datetime.now().strftime("%d %b %Y")

    if order["customer_email"]:
        body = f"""
        <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#fff;">
          {_header(company)}
          <div style="padding:30px;">
            <h2 style="color:#2e7d32;">Refund Processed Successfully</h2>
            <p style="color:#666;">Hi {order['customer_name']},</p>
            <p style="color:#666;">Your refund for order <strong>#{order['order_number']}</strong>
              has been successfully processed.</p>
            <div style="background:#e8f5e9;padding:15px;border-radius:8px;border-left:4px solid #2e7d32;margin:20px 0;">
              <p style="margin:0;"><strong>Refund Amount:</strong> {fmt_amount}</p>
              <p style="margin:5px 0 0;"><strong>Payment Method:</strong> {payment_method}</p>
              <p style="margin:5px 0 0;"><strong>Date:</strong> {fmt_date}</p>
            </div>
            <p style="color:#666;">Please allow 3-7 business days for the amount to reflect in your account.</p>
            <p style="color:#666;">Questions? <a href="mailto:{support}" style="color:#8b7355;">{support}</a></p>
          </div>
          {_footer(company, support)}
        </div>"""
        send_email(db, EmailCreate(
            name=order["customer_name"], email=order["customer_email"],
            subject=f"Refund Processed - Order #{order['order_number']}", message=body,
        ))

    if order["customer_phone"]:
        _notify(
            db, order, MT_REFUND_SUCCESS,
            named_vars={
                "customer_name":  order["customer_name"],
                "order_number":   order["order_number"],
                "refund_amount":  fmt_amount,
                "payment_method": payment_method,
                "refund_date":    fmt_date,
            },
            positional_order=[
                "customer_name", "order_number", "refund_amount",
                "payment_method", "refund_date",
            ],
            whatsapp_message=(
                f"Refund Processed!\n\n"
                f"Hi {order['customer_name']}, your refund of {fmt_amount} for "
                f"order #{order['order_number']} has been processed.\n"
                f"Method: {payment_method}\nDate: {fmt_date}\n"
                f"Allow 3-7 business days. — TWAM"
            ),
        )
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# ORDER_SHIPPED
# ═══════════════════════════════════════════════════════════════════════════════
def send_order_shipped_notification(db: Session, order_id: int) -> bool:
    order = _get_order_details(db, order_id)
    if not order:
        return False
    company       = _cfg(db, "COMPANY_NAME", "Only TWAM")
    support       = _cfg(db, "SUPPORT_EMAIL", "support@onlytwam.com")
    tracking_base = _cfg(db, "TRACKING_BASE_URL", "https://app.elite.ekartlogistics.in/track")
    tracking_url  = (f"{tracking_base}/{order['tracking_id']}" if order["tracking_id"] else "")

    if order["customer_email"]:
        track_btn = (
            f'<a href="{tracking_url}" style="display:inline-block;margin-top:15px;'
            f'background:#8b7355;color:#fff;padding:12px 30px;text-decoration:none;'
            f'border-radius:5px;">Track Your Package</a>' if tracking_url else ""
        )
        body = f"""
        <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#fff;">
          {_header(company)}
          <div style="padding:30px;">
            <h2 style="color:#333;margin:10px 0;">Your order is on its way!</h2>
            <p style="color:#666;">Hi {order['customer_name']},</p>
            <p style="color:#666;">Your order <strong>#{order['order_number']}</strong> has been shipped!</p>
            <div style="background:#f8f5f0;padding:20px;border-radius:8px;margin:25px 0;text-align:center;">
              <p style="margin:0 0 10px;color:#666;">Tracking ID</p>
              <p style="margin:0;font-size:24px;font-weight:bold;color:#8b7355;letter-spacing:2px;">
                {order['tracking_id'] or 'Processing...'}</p>
              {track_btn}
            </div>
          </div>
          {_footer(company, support)}
        </div>"""
        send_email(db, EmailCreate(
            name=order["customer_name"], email=order["customer_email"],
            subject=f"Your Order is On Its Way! #{order['order_number']}", message=body,
        ))

    if order["customer_phone"]:
        _notify(
            db, order, MT_ORDER_SHIPPED,
            named_vars={
                "customer_name": order["customer_name"],
                "order_number":  order["order_number"],
                "tracking_id":   order["tracking_id"] or "Processing",
            },
            positional_order=["customer_name", "order_number", "tracking_id"],
            whatsapp_message=(
                f"Order Shipped!\n\n"
                f"Hi {order['customer_name']}, your order #{order['order_number']} is on its way!\n"
                f"Tracking ID: {order['tracking_id'] or 'Coming soon'}\n"
                + (f"Track: {tracking_url}" if tracking_url else "") +
                f"\n\n— {company}"
            ),
        )
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# ORDER_OUT_FOR_DELIVERY
# ═══════════════════════════════════════════════════════════════════════════════
def send_out_for_delivery_notification(db: Session, order_id: int) -> bool:
    order = _get_order_details(db, order_id)
    if not order:
        return False
    company = _cfg(db, "COMPANY_NAME", "Only TWAM")
    support = _cfg(db, "SUPPORT_EMAIL", "support@onlytwam.com")

    if order["customer_email"]:
        body = f"""
        <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#fff;">
          {_header(company)}
          <div style="padding:30px;text-align:center;">
            <h2 style="color:#333;margin:10px 0;">Your order is arriving today!</h2>
            <p style="color:#666;font-size:16px;">Hi {order['customer_name']}, order
              <strong>#{order['order_number']}</strong> is out for delivery!</p>
            <div style="background:#e8f5e9;padding:20px;border-radius:8px;margin:25px 0;">
              <p style="margin:0;color:#2e7d32;font-size:16px;">
                Please ensure someone is available to receive the package.</p>
            </div>
            <p style="color:#666;"><strong>Tracking ID:</strong> {order['tracking_id'] or 'N/A'}</p>
          </div>
          {_footer(company, support)}
        </div>"""
        send_email(db, EmailCreate(
            name=order["customer_name"], email=order["customer_email"],
            subject=f"Arriving Today! Order #{order['order_number']}", message=body,
        ))

    if order["customer_phone"]:
        _notify(
            db, order, MT_OUT_FOR_DELIVERY,
            named_vars={
                "customer_name": order["customer_name"],
                "order_number":  order["order_number"],
            },
            positional_order=["customer_name", "order_number"],
            whatsapp_message=(
                f"Out for Delivery!\n\n"
                f"Hi {order['customer_name']}, your order #{order['order_number']} arrives today!\n"
                f"Please ensure someone is available.\n\n— {company}"
            ),
        )
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# ORDER_DELIVERED
# ═══════════════════════════════════════════════════════════════════════════════
def send_delivery_confirmation(
    db: Session, order_id: int, pod_image_url: Optional[str] = None
) -> bool:
    order = _get_order_details(db, order_id)
    if not order:
        return False
    company = _cfg(db, "COMPANY_NAME", "Only TWAM")
    support = _cfg(db, "SUPPORT_EMAIL", "support@onlytwam.com")
    website = _cfg(db, "WEBSITE_URL", "https://onlytwam.com")

    if order["customer_email"]:
        body = f"""
        <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#fff;">
          {_header(company)}
          <div style="padding:30px;text-align:center;">
            <h2 style="color:#2e7d32;margin:10px 0;">Your order has been delivered!</h2>
            <p style="color:#666;font-size:16px;">Hi {order['customer_name']}, order
              <strong>#{order['order_number']}</strong> has been successfully delivered.</p>
            <div style="background:#f8f5f0;padding:20px;border-radius:8px;margin:25px 0;">
              <p style="margin:0 0 15px;color:#333;">We hope you love your purchase!</p>
              <a href="{website}/account/orders-list"
                style="display:inline-block;background:#8b7355;color:#fff;
                       padding:12px 30px;text-decoration:none;border-radius:5px;">Leave a Review</a>
            </div>
            <p style="color:#999;font-size:14px;">Need to return? Visit "My Orders" within 7 days.</p>
          </div>
          {_footer(company, support)}
        </div>"""
        send_email(db, EmailCreate(
            name=order["customer_name"], email=order["customer_email"],
            subject=f"Delivered! Order #{order['order_number']} - {company}", message=body,
        ))

    if order["customer_phone"]:
        _notify(
            db, order, MT_ORDER_DELIVERED,
            named_vars={
                "customer_name": order["customer_name"],
                "order_number":  order["order_number"],
            },
            positional_order=["customer_name", "order_number"],
            whatsapp_message=(
                f"Delivered!\n\n"
                f"Hi {order['customer_name']}, your order #{order['order_number']} has been delivered!\n"
                f"Hope you love it! Questions? {support}\n\n— {company}"
            ),
        )
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# ORDER_CANCELLED
# ═══════════════════════════════════════════════════════════════════════════════
class CancelledBy(str, Enum):
    CUSTOMER  = "customer"
    ADMIN     = "admin"
    TECHNICAL = "technical"
    
def send_order_cancelled_notification(
    db: Session,
    order_id: int,
    reason: Optional[str] = None,
    cancelled_by: CancelledBy = CancelledBy.CUSTOMER,  # ← new param
) -> bool:
    order = _get_order_details(db, order_id)
    if not order:
        return False

    company = _cfg(db, "COMPANY_NAME", "Only TWAM")
    support = _cfg(db, "SUPPORT_EMAIL", "support@onlytwam.com")

    # ── Build context based on who/what cancelled ──────────────────────────
    if cancelled_by == CancelledBy.ADMIN:
        email_heading  = "Your Order Has Been Cancelled by Our Team"
        email_intro    = (
            f"We're sorry to inform you that your order "
            f"<strong>#{order['order_number']}</strong> has been cancelled by our team"
            f"{f' — {reason}' if reason else ''}."
        )
        whatsapp_intro = (
            f"Hi {order['customer_name']}, your order #{order['order_number']} "
            f"was cancelled by our team{f': {reason}' if reason else ''}."
        )
        refund_note    = "If you paid online, your refund will be processed within 5-7 business days."

    elif cancelled_by == CancelledBy.TECHNICAL:
        email_heading  = "Your Order Was Cancelled Due to a Technical Issue"
        email_intro    = (
            f"We sincerely apologise — your order "
            f"<strong>#{order['order_number']}</strong> was cancelled due to a technical issue"
            f"{f' ({reason})' if reason else ''}. "
            f"Please re-place your order or contact us for assistance."
        )
        whatsapp_intro = (
            f"Hi {order['customer_name']}, sorry! Order #{order['order_number']} "
            f"was cancelled due to a technical issue{f' ({reason})' if reason else ''}. "
            f"Please re-order or contact us."
        )
        refund_note    = (
            "If you were charged, your full refund will be processed within 3-5 business days. "
            "We apologise for the inconvenience."
        )

    else:  # CancelledBy.CUSTOMER (existing behaviour)
        email_heading  = "Order Cancelled"
        email_intro    = (
            f"Your order <strong>#{order['order_number']}</strong> has been cancelled"
            f"{f' — {reason}' if reason else ''}."
        )
        whatsapp_intro = (
            f"Hi {order['customer_name']}, your order #{order['order_number']} has been cancelled."
        )
        refund_note    = "If you paid online, your refund will be processed within 5-7 business days."

    # ── Email ──────────────────────────────────────────────────────────────
    if order["customer_email"]:
        reason_line = (
            f'<p style="color:#666;"><strong>Reason:</strong> {reason}</p>'
            if reason else ""
        )
        body = f"""
        <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#fff;">
          {_header(company)}
          <div style="padding:30px;">
            <h2 style="color:#333;">{email_heading}</h2>
            <p style="color:#666;">Hi {order['customer_name']},</p>
            <p style="color:#666;">{email_intro}</p>
            {reason_line}
            <div style="background:#f8f5f0;padding:15px;border-radius:8px;margin:20px 0;">
              <p style="margin:0;"><strong>Order Number:</strong> #{order['order_number']}</p>
              <p style="margin:5px 0 0;"><strong>Amount:</strong> {_fmt(order['total_amount'])}</p>
            </div>
            <p style="color:#666;">{refund_note}</p>
          </div>
          {_footer(company, support)}
        </div>"""

        send_email(db, EmailCreate(
            name=order["customer_name"],
            email=order["customer_email"],
            subject=f"{email_heading} - #{order['order_number']}",
            message=body,
        ))

    # ── WhatsApp / SMS ─────────────────────────────────────────────────────
    if order["customer_phone"]:
        _notify(
            db, order, MT_ORDER_CANCELLED,
            named_vars={
                "customer_name": order["customer_name"],
                "order_number":  order["order_number"],
                "total_amount":  _fmt(order["total_amount"]),
                "cancelled_by":  cancelled_by.value,          # ← pass to template
                "reason":        reason or "",
            },
            positional_order=[
                "customer_name", "order_number",
                "total_amount", "cancelled_by", "reason",
            ],
            whatsapp_message=(
                f"Order Cancelled\n\n"
                f"{whatsapp_intro}\n"
                f"Amount: {_fmt(order['total_amount'])}\n"
                f"{refund_note}\n"
                f"Questions? {support} — TWAM"
            ),
        )

    return True


# ═══════════════════════════════════════════════════════════════════════════════
# RETURN_INITIATED
# ═══════════════════════════════════════════════════════════════════════════════
def send_return_initiated_notification(
    db: Session, order_id: int, return_reason: Optional[str] = None
) -> bool:
    order = _get_order_details(db, order_id)
    if not order:
        return False
    company = _cfg(db, "COMPANY_NAME", "Only TWAM")
    support = _cfg(db, "SUPPORT_EMAIL", "support@onlytwam.com")

    if order["customer_email"]:
        reason_line = (f'<p style="color:#666;"><strong>Reason:</strong> {return_reason}</p>' if return_reason else "")
        body = f"""
        <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#fff;">
          {_header(company)}
          <div style="padding:30px;">
            <h2 style="color:#333;">Return Request Received</h2>
            <p style="color:#666;">Hi {order['customer_name']},</p>
            <p style="color:#666;">We've received your return request for order
              <strong>#{order['order_number']}</strong>.</p>
            {reason_line}
            <div style="background:#fff8e7;padding:15px;border-radius:8px;border-left:4px solid #f5a623;margin:20px 0;">
              <h4 style="margin:0 0 10px;color:#333;">What happens next?</h4>
              <ol style="margin:0;padding-left:20px;color:#666;">
                <li>Our team will review your request</li>
                <li>You'll receive pickup details within 24-48 hours</li>
                <li>Refund will be processed after we receive the item</li>
              </ol>
            </div>
            <p style="color:#666;">Questions? <a href="mailto:{support}" style="color:#8b7355;">{support}</a></p>
          </div>
          {_footer(company, support)}
        </div>"""
        send_email(db, EmailCreate(
            name=order["customer_name"], email=order["customer_email"],
            subject=f"Return Request Received - #{order['order_number']}", message=body,
        ))

    if order["customer_phone"]:
        _notify(
            db, order, MT_RETURN_INITIATED,
            named_vars={
                "customer_name": order["customer_name"],
                "order_number":  order["order_number"],
            },
            positional_order=["customer_name", "order_number"],
            whatsapp_message=(
                f"Return Requested\n\n"
                f"Hi {order['customer_name']}, we received your return for #{order['order_number']}.\n"
                f"Pickup details in 24-48 hrs. Questions? {support} — TWAM"
            ),
        )
    return True


# ── Generic status dispatcher ─────────────────────────────────────────────────

def send_order_status_notification(db: Session, order_id: int, new_status: str) -> bool:
    try:
        if new_status == "Confirmed":
            return send_order_placed_notification(db, order_id)
        elif new_status == "Pending":
            return send_order_pending_notification(db, order_id)
        elif new_status in ("Shipped", "Processing"):
            return send_order_shipped_notification(db, order_id)
        elif new_status == "Out for Delivery":
            return send_out_for_delivery_notification(db, order_id)
        elif new_status == "Delivered":
            return send_delivery_confirmation(db, order_id)
        elif new_status == "Cancelled":
            return send_order_cancelled_notification(db, order_id)
        elif new_status == "Return Initiated":
            return send_return_initiated_notification(db, order_id)
    except Exception as e:
        print(f"[Notifications] send_order_status_notification failed for order {order_id}: {e}")
    return True