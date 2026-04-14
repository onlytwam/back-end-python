"""
app/ekart/notifications.py
───────────────────────────
SMS and email notifications for all order lifecycle events.

SMS credential source:  sms.SmsCredential  (was: twam.AppSettings SMS_* keys)
SMS template source:    sms.SmsMessageTemplate  (was: mdm.MessageTemplate)
SMS audit log:          sms.SmsLog

VARIABLE MAPPING  (SmartPing portal uses ${var1}, ${var2}, … positional vars)
──────────────────────────────────────────────────────────────────────────────
Template             | var1          | var2         | var3          | var4           | var5
─────────────────────|───────────────|──────────────|───────────────|────────────────|──────────────
OTP                  | otp           |              |               |                |
WELCOME              | (no vars)     |              |               |                |
ORDER_PLACED         | customer_name | order_number | items         | total_amount   | delivery_address
ORDER_PENDING        | customer_name | order_number | total_amount  |                |
PAYMENT_FAILURE      | customer_name | order_number | total_amount  |                |
REFUND_AMOUNT        | refund_amount | order_number | refund_date   |                |
TEMPORARY_PASSWORD   | customer_name | password     |               |                |
"""

import time
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import text

from app.sendemail.repository import send_email
from app.sendemail.schemas import EmailCreate
from app.sms.models import SmsCredential, SmsMessageTemplate, SmsLog


# ── AppSettings cache (5-minute TTL) — still used for non-SMS config ─────────

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

_cred_cache: Optional[dict] = None
_cred_ts: float = 0.0
_CRED_TTL = 300


def _get_sms_credential(db: Session) -> Optional[dict]:
    """
    Return active SMS credential from sms.SmsCredential.
    Cached for 5 minutes. Returns None if no credential row exists.
    """
    global _cred_cache, _cred_ts
    if time.time() - _cred_ts < _CRED_TTL and _cred_cache is not None:
        return _cred_cache
    try:
        row = db.query(SmsCredential).filter(
            SmsCredential.isActive   == True,
            SmsCredential.deletedInd == False,
        ).first()
        if row:
            _cred_cache = {
                "apiUrl":      row.apiUrl      or "",
                "apiMultiUrl": row.apiMultiUrl or row.apiUrl or "",
                "username":    row.username    or "",
                "password":    row.password    or "",
                "senderId":    row.senderId    or "TWAMCH",
                "unicode":     row.unicode     or "false",
            }
        else:
            _cred_cache = {}
        _cred_ts = time.time()
    except Exception as e:
        print(f"[Notifications] Could not load SmsCredential: {e}")
        _cred_cache = {}
        _cred_ts = time.time()
    return _cred_cache if _cred_cache else None


# ── SmsMessageTemplate cache (5-minute TTL) ──────────────────────────────────

_template_cache: dict = {}
_template_ts: float = 0.0
_TEMPLATE_TTL = 300


def _get_all_templates(db: Session) -> dict:
    global _template_cache, _template_ts
    if time.time() - _template_ts < _TEMPLATE_TTL and _template_cache:
        return _template_cache
    try:
        rows = (
            db.query(SmsMessageTemplate)
            .filter(
                SmsMessageTemplate.deletedInd == False,
                SmsMessageTemplate.state.ilike("active"),
            )
            .all()
        )
        _template_cache = {
            row.messageType: {
                "messageContent": row.messageContent or "",
                "dltId":          row.dltId          or "",   # Principal Entity ID
                "templateId":     row.templateId     or "",   # DLT Content ID
            }
            for row in rows
            if row.messageType
        }
        _template_ts = time.time()
    except Exception as e:
        print(f"[Notifications] Could not load SmsMessageTemplate: {e}")
        _template_cache = {}
        _template_ts = time.time()
    return _template_cache


def _get_template(db: Session, message_type: str) -> Optional[dict]:
    return _get_all_templates(db).get(message_type)


def _render_template(content: str, variables: list) -> str:
    """
    Replace SmartPing-style positional placeholders ${var1}, ${var2}, …
    with the values list in order.  variables[0] → ${var1}, etc.
    """
    for i, value in enumerate(variables, start=1):
        placeholder = "${var" + str(i) + "}"
        content = content.replace(placeholder, str(value) if value is not None else "")
    return content


# MessageType constants — must match messageType column in sms.SmsMessageTemplate
MT_ORDER_PLACED      = "ORDER_PLACED"
MT_ORDER_SHIPPED     = "ORDER_SHIPPED"
MT_OUT_FOR_DELIVERY  = "ORDER_OUT_FOR_DELIVERY"
MT_ORDER_DELIVERED   = "ORDER_DELIVERED"
MT_ORDER_CANCELLED   = "ORDER_CANCELLED"
MT_RETURN_INITIATED  = "RETURN_INITIATED"
MT_ORDER_PENDING     = "ORDER_PENDING"
MT_PAYMENT_FAILURE   = "PAYMENT_FAILURE"
MT_REFUND_AMOUNT     = "REFUND_AMOUNT"
MT_WELCOME           = "WELCOME"
MT_OTP               = "OTP"
MT_TEMPORARY_PASSWORD = "TEMPORARY_PASSWORD"


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
    """Write one row to sms.SmsLog via ORM. Never raises."""
    try:
        db.add(SmsLog(
            recipient            = recipient,
            messageType          = message_type,
            messageContent       = message_content,
            templateId           = template_id,
            dltPrincipalEntityId = dlt_peid,
            transactionId        = transaction_id,
            state                = state,
            statusCode           = status_code,
            errorDesc            = error_desc,
        ))
        db.commit()
    except Exception as e:
        print(f"[SMS Log] Failed to write SmsLog: {e}")


# ── SMS sender ────────────────────────────────────────────────────────────────
#
# SmartPing uses positional placeholders: ${var1}, ${var2}, …
# Pass variables as an ORDERED LIST matching the SmartPing template order.
#
# Column semantics:
#   templateId → dltContentId          (DLT Content/Template ID)
#   dltId      → dltPrincipalEntityId  (Principal Entity ID)

def _send_sms(db: Session, phone: str, message_type: str, variables: list) -> bool:
    """
    Send SMS via SmartPing HTTP API.

    ROOT CAUSE OF ERROR 5307 (confirmed from SmsLog CSV analysis):
    ──────────────────────────────────────────────────────────────
    SmartPing is a DLT aggregator. When you pass a "text" param in the API
    call, SmartPing forwards THAT exact text to the telecom operator.
    The telecom then scrubs it against the TRAI DLT-registered template
    (looked up via dltContentId). If our "text" content differs even by
    one space/newline from the TRAI-registered version → Error 5307.

    The CORRECT approach:
      • Do NOT send "text" param at all.
      • Pass only: username, password, unicode, from, to,
                   dltContentId, dltPrincipalEntityId, var1, var2, …
      • SmartPing fetches their internally stored template by dltContentId,
        substitutes ${varN} → your varN param value, sends to telecom.
      • Telecom verifies against TRAI DLT using dltContentId → MATCHES.

    Evidence from SmsLog:
      Rows 1-4:  text sent with spaces (single-line) → DELIVERY_FAILED 5307
      Rows 5-7:  text sent with "{otp}" literal (not substituted) → 5307
      Rows 8-9:  text sent with newlines (multiline) → still 5307
      All failed because WE controlled the text. Fix: let SmartPing control it.

    variables: ordered list — variables[0] → var1, variables[1] → var2, …
    """
    cred = _get_sms_credential(db)
    if not cred or not cred.get("apiUrl") or not cred.get("username"):
        print(f"[SMS] No active SmsCredential found — skipping {message_type}")
        return False

    template = _get_template(db, message_type)
    if not template:
        print(f"[SMS] No active template for '{message_type}' in sms.SmsMessageTemplate")
        return False

    dlt_content_id  = template["templateId"]
    dlt_peid        = template["dltId"]
    message_content = template["messageContent"]

    if not dlt_content_id:
        print(f"[SMS] Template '{message_type}' has no TemplateId (DLT Content ID)")
        return False

    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) < 10:
        print(f"[SMS] Invalid phone: {phone}")
        return False
    # SmartPing requires full 12-digit number: country code 91 + 10-digit number
    recipient = "91" + digits[-10:]

    # Rendered text is ONLY used for our SmsLog — never sent to SmartPing
    log_text = _render_template(message_content, variables)

    print(f"[SMS] Sending {message_type} → {recipient}")
    print(f"[SMS] dltContentId={dlt_content_id} | dltPEID={dlt_peid}")
    for i, v in enumerate(variables, start=1):
        print(f"[SMS] var{i}={v}")

    try:
        import httpx

        # ═══════════════════════════════════════════════════════════════════════
        # CRITICAL FIX FOR ERROR 5307: DO NOT send "text" parameter!
        # ═══════════════════════════════════════════════════════════════════════
        # SmartPing's DLT API fetches the template internally using dltContentId.
        # Sending "text" causes Error 5307 because it never matches exactly due to
        # encoding differences, whitespace normalization, or internal formatting.
        # 
        # The correct approach:
        #   ✓ Send dltContentId - SmartPing knows which template to use
        #   ✓ Send var1, var2, ... - SmartPing substitutes ${varN} placeholders
        #   ✗ DO NOT send "text" - SmartPing has the template already
        # ═══════════════════════════════════════════════════════════════════════
        
        params = {
            "username":             cred["username"],
            "password":             cred["password"],
            "from":                 cred["senderId"],
            "to":                   recipient,
            "dltContentId":         dlt_content_id,
            "dltPrincipalEntityId": dlt_peid,
        }
        
        # Only include unicode if it's set to "true" (for Unicode messages)
        # Many APIs don't need this parameter for English messages
        if cred.get("unicode") and cred["unicode"].lower() == "true":
            params["unicode"] = "true"

        # var1, var2, … — SmartPing substitutes these into ${var1}, ${var2}…
        for i, value in enumerate(variables, start=1):
            params[f"var{i}"] = str(value) if value is not None else ""

        response = httpx.get(cred["apiUrl"], params=params, timeout=10)
        raw      = response.text.strip()
        print(f"[SMS] HTTP {response.status_code} | {raw[:300]}")

        # ── Parse response safely ─────────────────────────────────────────────
        # SmartPing may return JSON, plain-text, or an empty body depending on
        # whether the request was valid. Never call .json() blindly.
        data = {}
        if raw:
            try:
                data = response.json()
            except Exception:
                # Plain-text response e.g. "success", "Message Submitted", or
                # an HTML error page. Treat as success only if HTTP 200.
                data = {"state": "SUBMIT_ACCEPTED" if response.status_code == 200 else "ERROR",
                        "description": raw[:200]}

        ok          = data.get("statusCode") == 200 or data.get("state") == "SUBMIT_ACCEPTED"
        txn_id      = str(data.get("transactionId", ""))
        state_str   = data.get("state", "UNKNOWN")
        status_code = int(data.get("statusCode") or response.status_code or 0)
        error_desc  = "" if ok else (data.get("description") or raw[:200])

        _log_sms(db, recipient, message_type, log_text,
                 dlt_content_id, dlt_peid, txn_id, state_str, status_code, error_desc)

        if ok:
            print(f"[SMS] ✅ {message_type} → {recipient} txId={txn_id}")
        else:
            print(f"[SMS] ❌ {message_type} FAILED statusCode={status_code} state={state_str} desc={error_desc}")
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


# ── Notification router: WhatsApp-first when opted in ─────────────────────────

def _notify(db: Session, order: dict, message_type: str,
            sms_variables: list, whatsapp_message: str,
            media_url: str = "") -> None:
    """
    Route order notification:
      - WhatsApp if customer opted in (IsWhatsappNotification=True).
      - SMS otherwise.
    sms_variables: ordered list matching ${var1}, ${var2}, … in SmartPing template.
    """
    phone = order.get("customer_phone") or ""
    if not phone:
        return
    if order.get("whatsapp_opted_in"):
        _send_whatsapp(db, phone, whatsapp_message, message_type, media_url)
    else:
        _send_sms(db, phone, message_type, sms_variables)


# ── OTP / Welcome / Temporary Password ───────────────────────────────────────

def send_otp_sms(db: Session, phone: str, otp: str) -> bool:
    """
    OTP template:  Only TWAM Login OTP: ${var1}
    var1 = otp
    """
    if not phone:
        return False
    return _send_sms(db, phone, MT_OTP, [otp])


def send_welcome_sms(db: Session, phone: str) -> bool:
    """
    WELCOME template has no variables.
    """
    if not phone:
        return False
    return _send_sms(db, phone, MT_WELCOME, [])


def send_temporary_password_sms(db: Session, phone: str, name: str, password: str) -> bool:
    """
    TEMPORARY_PASSWORD template:  Hi ${var1} Your Temporary Password is : ${var2}
    var1 = customer name, var2 = password
    """
    if not phone:
        return False
    return _send_sms(db, phone, MT_TEMPORARY_PASSWORD, [name, password])


# ── Order Placed ──────────────────────────────────────────────────────────────
# SmartPing template Order-Place (1360573):
#   Dear ${var1}, Thank you for your order! Order Details: Order ID:${var2}
#   Items:${var3} Total Amount:${var4} Delivery Address:${var5}
#   var1=customer_name, var2=order_number, var3=items, var4=total_amount, var5=delivery_address

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
        delivery_address = " ".join(filter(None, [order["address_line"], order["city"], order["pincode"]]))
        _notify(db, order, MT_ORDER_PLACED,
            sms_variables=[
                order["customer_name"],
                order["order_number"],
                _fmt_items_sms(items),
                _fmt(order["total_amount"]),
                delivery_address,
            ],
            whatsapp_message=(
                f"Order Confirmed!\n\n"
                f"Hi {order['customer_name']}, your order #{order['order_number']} has been placed.\n"
                f"Amount: {_fmt(order['total_amount'])}\n"
                f"Delivery to: {delivery_address}\n\n"
                f"We'll notify you when it ships! — TWAM"
            ),
        )

    return True


# ── Order Pending ─────────────────────────────────────────────────────────────
# SmartPing template Order-Pending (1360584):
#   Dear${var1}, Your Order (Order ID:${var2}) is currently Pending.
#   Total Amount:${var3} We're processing your order…
#   var1=customer_name, var2=order_number, var3=total_amount

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
        _notify(db, order, MT_ORDER_PENDING,
            sms_variables=[
                order["customer_name"],
                order["order_number"],
                _fmt(order["total_amount"]),
            ],
            whatsapp_message=(
                f"Payment Pending\n\n"
                f"Hi {order['customer_name']}, your order #{order['order_number']} is pending payment.\n"
                f"Amount: {_fmt(order['total_amount'])}\n\nWe'll update you once confirmed. — TWAM"
            ),
        )
    return True


# ── Payment Failure ───────────────────────────────────────────────────────────
# SmartPing template Payment-Failure (1360574):
#   Dear ${var1}, Unfortunately, Your Order(Order ID:${var2}) Could not be processed
#   due to a payment failure. Total Amount:${var3} …
#   var1=customer_name, var2=order_number, var3=total_amount

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
        _notify(db, order, MT_PAYMENT_FAILURE,
            sms_variables=[
                order["customer_name"],
                order["order_number"],
                _fmt(amount),
            ],
            whatsapp_message=(
                f"Payment Failed\n\n"
                f"Hi {order['customer_name']}, payment for order #{order['order_number']} failed.\n"
                f"Amount: {_fmt(amount)}\n"
                f"Please retry or use a different method.\n\nRefund if deducted: 3-7 days. — TWAM"
            ),
        )
    return True


# ── Refund Amount ─────────────────────────────────────────────────────────────
# SmartPing template Refund-Amount (1360572):
#   We're Pleased to inform you that a refund of Rs. ${var1} for Order ${var2}
#   has been successfully processed using your original payment method on ${var3}. …
#   var1=refund_amount, var2=order_number, var3=refund_date

def send_refund_amount_notification(
    db: Session, order_id: int, refund_amount: float, refund_date: Optional[str] = None
) -> bool:
    order = _get_order_details(db, order_id)
    if not order or not order["customer_phone"]:
        return False
    return _send_sms(db, order["customer_phone"], MT_REFUND_AMOUNT, [
        _fmt(refund_amount),
        order["order_number"],
        refund_date or datetime.now().strftime("%d %b %Y"),
    ])


# ── Order Shipped ─────────────────────────────────────────────────────────────
# No SmartPing template shown in screenshots — keeping generic 3-var pattern
# var1=customer_name, var2=order_number, var3=tracking_id
# Add this template in SmartPing if you want SMS on shipment.

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
            <div style="text-align:center;margin-bottom:30px;">
              <h2 style="color:#333;margin:10px 0;">Your order is on its way!</h2>
            </div>
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
        _notify(db, order, MT_ORDER_SHIPPED,
            sms_variables=[
                order["customer_name"],
                order["order_number"],
                order["tracking_id"] or "Processing",
            ],
            whatsapp_message=(
                f"Order Shipped!\n\n"
                f"Hi {order['customer_name']}, your order #{order['order_number']} is on its way!\n"
                f"Tracking ID: {order['tracking_id'] or 'Coming soon'}\n"
                + (f"Track: {tracking_url}" if tracking_url else "") +
                f"\n\n— {company}"
            ),
        )
    return True


# ── Out for Delivery ──────────────────────────────────────────────────────────
# var1=customer_name, var2=order_number

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
        _notify(db, order, MT_OUT_FOR_DELIVERY,
            sms_variables=[
                order["customer_name"],
                order["order_number"],
            ],
            whatsapp_message=(
                f"Out for Delivery!\n\n"
                f"Hi {order['customer_name']}, your order #{order['order_number']} arrives today!\n"
                f"Please ensure someone is available.\n\n— {company}"
            ),
        )
    return True


# ── Delivered ─────────────────────────────────────────────────────────────────
# var1=customer_name, var2=order_number

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
        _notify(db, order, MT_ORDER_DELIVERED,
            sms_variables=[
                order["customer_name"],
                order["order_number"],
            ],
            whatsapp_message=(
                f"Delivered!\n\n"
                f"Hi {order['customer_name']}, your order #{order['order_number']} has been delivered!\n"
                f"Hope you love it! Questions? {support}\n\n— {company}"
            ),
        )
    return True


# ── Order Cancelled ───────────────────────────────────────────────────────────
# var1=customer_name, var2=order_number, var3=total_amount

def send_order_cancelled_notification(
    db: Session, order_id: int, reason: Optional[str] = None
) -> bool:
    order = _get_order_details(db, order_id)
    if not order:
        return False
    company = _cfg(db, "COMPANY_NAME", "Only TWAM")
    support = _cfg(db, "SUPPORT_EMAIL", "support@onlytwam.com")

    if order["customer_email"]:
        reason_line = (f'<p style="color:#666;"><strong>Reason:</strong> {reason}</p>' if reason else "")
        body = f"""
        <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#fff;">
          {_header(company)}
          <div style="padding:30px;">
            <h2 style="color:#333;">Order Cancelled</h2>
            <p style="color:#666;">Hi {order['customer_name']},</p>
            <p style="color:#666;">Your order <strong>#{order['order_number']}</strong> has been cancelled
              {f"— {reason}" if reason else ""}.</p>
            {reason_line}
            <div style="background:#f8f5f0;padding:15px;border-radius:8px;margin:20px 0;">
              <p style="margin:0;"><strong>Order Number:</strong> #{order['order_number']}</p>
              <p style="margin:5px 0 0;"><strong>Amount:</strong> {_fmt(order['total_amount'])}</p>
            </div>
            <p style="color:#666;">If you paid online, your refund will be processed within 5-7 business days.</p>
          </div>
          {_footer(company, support)}
        </div>"""
        send_email(db, EmailCreate(
            name=order["customer_name"], email=order["customer_email"],
            subject=f"Order Cancelled - #{order['order_number']}", message=body,
        ))

    if order["customer_phone"]:
        _notify(db, order, MT_ORDER_CANCELLED,
            sms_variables=[
                order["customer_name"],
                order["order_number"],
                _fmt(order["total_amount"]),
            ],
            whatsapp_message=(
                f"Order Cancelled\n\n"
                f"Hi {order['customer_name']}, your order #{order['order_number']} has been cancelled.\n"
                f"Amount: {_fmt(order['total_amount'])}\n"
                f"Refund if paid: 5-7 days. Questions? {support} — TWAM"
            ),
        )
    return True


# ── Return Initiated ──────────────────────────────────────────────────────────
# var1=customer_name, var2=order_number

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
        _notify(db, order, MT_RETURN_INITIATED,
            sms_variables=[
                order["customer_name"],
                order["order_number"],
            ],
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