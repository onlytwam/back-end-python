"""
app/ekart/notifications.py
───────────────────────────
SMS and email notifications for all order lifecycle events.

SMS credential source:  sms.SmsCredential
SMS template source:    sms.SmsMessageTemplate
SMS audit log:          sms.SmsLog

DB TEMPLATE PLACEHOLDERS — SmartPing supports both {#var#} and ${var1} styles
──────────────────────────────────────────────────────────────────────────────────────────
DB MessageType  | DB template uses   | positional_order (var1, var2, …)
────────────────|────────────────────|──────────────────────────────────────────────────
OTP             | {#var#}            | var1=otp
WELCOME         | (no placeholders)  | (no vars)
ORDER_PLACED    | {#var#}…{#var#}    | var1=customer_name, var2=order_number, var3=items,
                |                    | var4=total_amount, var5=delivery_address
ORDER_PENDING   | {#var#}…{#var#}    | var1=customer_name, var2=order_number, var3=total_amount
PAYMENT_FAILURE | {#var#}…{#var#}    | var1=customer_name, var2=order_number, var3=total_amount
REFUND_AMOUNT   | {#var#}…{#var#}    | var1=refund_amount, var2=order_number, var3=refund_date
REFUND_SUCCESS  | {#var#}…{#var#}    | var1=customer_name, var2=order_number, var3=refund_amount,
                |                    | var4=payment_method, var5=refund_date
TEMP_PASSWORD   | {#var#}, {#var#}   | var1=customer_name, var2=temp_password

HOW _send_sms RESOLVES VARIABLES
──────────────────────────────────
1. named_vars  : {"otp": "835460", …}            — human-readable key → actual value
2. positional_order: ["otp"]                      — maps position → key
   → var1 = named_vars["otp"] = "835460"
3. _render_template replaces {#var#} one-by-one in order → "835460"
4. rendered text is sent as `text` param to SmartPing
5. var1="835460" also sent as query param for DLT validation

SmartPing HTTP GET /fe/api/v1/send
───────────────────────────────────
Required params: username, password, from, to, text, dltContentId, dltPrincipalEntityId
Variable params: var1, var2, var3, …

KEY FIX vs previous version
────────────────────────────
BEFORE (broken):
  - API was called TWICE — first call used raw {#var#} template (unrendered)
  - Second call rendered text correctly but its result was ignored for logging
  - {#var#} was never replaced in code — SmartPing delivered literal {#var#}

AFTER (fixed):
  - Text is rendered ONCE before building params
  - API is called ONCE with correctly rendered text
  - _render_template handles {#var#}, ${var1}, and {var1} formats
"""

import re
import time
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import Enum, text

from app.sendemail.repository import send_email
from app.sendemail.schemas import EmailCreate
from app.shared.email_template import (
    twam_email, greeting, info_box, items_table,
    totals_block, cta_button, section_divider,
)


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


# ════════════════════════════════════════════════════════════════════════════════
# TEMPLATE RENDERING — handles ALL three placeholder formats
#
#  Format 1: {#var#}   — TRAI DLT / SmartPing DB template style  ← FIXED
#  Format 2: ${var1}   — SmartPing positional style
#  Format 3: {var1}    — fallback positional style
#
#  Algorithm:
#    Step 1 — Replace each {#var#} occurrence in left-to-right order
#             with the value at that position in positional_order.
#             e.g. first {#var#} → var1 value, second → var2 value, etc.
#    Step 2 — Replace ${var1}, ${var2}, … with positional values.
#    Step 3 — Replace {var1}, {var2}, … with positional values (fallback).
# ════════════════════════════════════════════════════════════════════════════════

def _render_template(content: str, positional_order: list, named_vars: dict) -> str:
    """
    Render a DB template string by substituting all placeholder styles.

    Supports:
      {#var#}   — TRAI DLT format (SmartPing DB templates use this)
      ${var1}   — SmartPing positional format
      {var1}    — fallback positional format

    Thread-safe: uses iter() instead of nonlocal counter so concurrent
    FastAPI requests each get their own iterator — no shared mutable state.

    Example:
        content          = "Only TWAM Login OTP: {#var#}\\nValid for 10 mins."
        positional_order = ["otp"]
        named_vars       = {"otp": "835460"}
        → "Only TWAM Login OTP: 835460\\nValid for 10 mins."

    Example (multiple vars):
        content          = "Hi {#var#} your order {#var#} is confirmed."
        positional_order = ["customer_name", "order_number"]
        named_vars       = {"customer_name": "Eshwar", "order_number": "ORD-001"}
        → "Hi Eshwar your order ORD-001 is confirmed."
    """
    values = [str(named_vars.get(key, "") or "") for key in positional_order]

    # ── Step 1: Replace {#var#} one at a time in order ───────────────────────
    # Use iter() so each call to this function gets its own independent iterator.
    # This is thread-safe — no shared nonlocal/global counter.
    value_iter = iter(values)

    def replace_dlt_var(match) -> str:
        return next(value_iter, "")   # returns "" if more {#var#} than values

    content = re.sub(r'\{#var#\}', replace_dlt_var, content)

    # ── Step 2: Replace ${var1}, ${var2}, … ──────────────────────────────────
    for i, value in enumerate(values, start=1):
        content = content.replace(f"${{var{i}}}", value)

    # ── Step 3: Replace {var1}, {var2}, … (fallback) ─────────────────────────
    for i, value in enumerate(values, start=1):
        content = content.replace(f"{{var{i}}}", value)

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
        SELECT p."Name", oi."Quantity", oi."Price", pv."Color", s."SizeLabel", pvd."StockId"
        FROM twam."OrderItems" oi
        LEFT JOIN twam."Products" p ON p."ProductId" = oi."ProductId"
        LEFT JOIN twam."ProductVariantDetail" pvd
               ON pvd."ProductVariantDetailId" = oi."ProductVariantDetailId"
        LEFT JOIN twam."ProductVariants" pv ON pv."ProductVariantId" = pvd."ProductVariantId"
        LEFT JOIN mdm."Size" s ON s."SizeId" = pvd."Size"
        WHERE oi."OrderId" = :order_id
          AND oi."DeletedInd" = false
          AND (oi."IsReturn" IS NULL OR oi."IsReturn" = false)
    """), {"order_id": order_id}).fetchall()
    return [
        {"name": r[0] or "Product", "quantity": r[1] or 1,
         "price": float(r[2]) if r[2] else 0, "color": r[3], "size": r[4], "stock_id": r[5]}
        for r in rows
    ]


def _fmt(amount: float) -> str:
    return f"Rs.{amount:,.2f}"


def _fmt_items_sms(items: list) -> str:
    return ", ".join(f"{i['name']} x{i['quantity']}" for i in items) or "Items"


# ── Email HTML helpers ────────────────────────────────────────────────────────

def _header(company_name: str) -> str:
    """Kept for backward-compat; new code uses twam_email() directly."""
    return ""


def _footer(company_name: str, support_email: str) -> str:
    """Kept for backward-compat; new code uses twam_email() directly."""
    return ""


def _items_table(items: list) -> str:
    rows_html = ""
    for item in items:
        details = []
        if item.get("size"):  details.append(f"Size: {item['size']}")
        if item.get("color"): details.append(f"Color: {item['color']}")
        det = (f"<br/><small style='color:#6B5060;font-size:11px'>{' | '.join(details)}</small>"
               if details else "")
        total = item["price"] * item["quantity"]
        rows_html += f"""
        <tr>
          <td style="padding:14px 16px;border-bottom:1px solid #F0E6D0;
                     font-family:Georgia,serif;font-size:13px;color:#2C1018;">
            {item['name']}{det}
          </td>
          <td style="padding:14px 16px;border-bottom:1px solid #F0E6D0;
                     text-align:center;font-family:Georgia,serif;font-size:13px;color:#6B5060;">
            {item['quantity']}
          </td>
          <td style="padding:14px 16px;border-bottom:1px solid #F0E6D0;
                     text-align:right;font-family:Georgia,serif;font-size:13px;color:#6B5060;">
            {_fmt(item['price'])}
          </td>
          <td style="padding:14px 16px;border-bottom:1px solid #F0E6D0;
                     text-align:right;font-family:Georgia,serif;font-size:14px;
                     font-weight:600;color:#2C1018;">
            {_fmt(total)}
          </td>
        </tr>"""
    return f"""
    <table style="width:100%;border-collapse:collapse;margin-bottom:0;">
      <thead>
        <tr style="background:#6A012A;">
          <th style="padding:10px 16px;text-align:left;font-family:Georgia,serif;
                     font-size:9px;letter-spacing:0.24em;text-transform:uppercase;
                     color:rgba(246,238,225,0.7);font-weight:normal;">Product</th>
          <th style="padding:10px 16px;text-align:center;font-family:Georgia,serif;
                     font-size:9px;letter-spacing:0.24em;text-transform:uppercase;
                     color:rgba(246,238,225,0.7);font-weight:normal;">Qty</th>
          <th style="padding:10px 16px;text-align:right;font-family:Georgia,serif;
                     font-size:9px;letter-spacing:0.24em;text-transform:uppercase;
                     color:rgba(246,238,225,0.7);font-weight:normal;">Price</th>
          <th style="padding:10px 16px;text-align:right;font-family:Georgia,serif;
                     font-size:9px;letter-spacing:0.24em;text-transform:uppercase;
                     color:rgba(246,238,225,0.7);font-weight:normal;">Total</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>"""


def _build_invoice_email(order: dict, items: list, company: str, support: str) -> str:
    """Generate an invoice-style HTML email that mirrors the Invoice.tsx design."""
    subtotal    = sum(i["price"] * i["quantity"] for i in items)
    ship_charge = order.get("delivery_charge") or 0
    coupon      = order.get("coupon_amount") or 0
    total       = order.get("total_amount") or 0
    order_date  = datetime.now().strftime("%d %B %Y")
    order_no    = order.get("order_number", "")

    # Address block
    addr_line = ", ".join(filter(None, [
        order.get("address_line"), order.get("locality"),
        order.get("city"), order.get("state"), order.get("pincode"),
    ]))
    addr_html = f"""
      <div style="margin-bottom:28px;padding-bottom:24px;border-bottom:1.5px solid #EDE0CC;">
        <div style="font-family:Georgia,serif;font-size:9px;letter-spacing:0.28em;
                    text-transform:uppercase;color:#9B7080;margin-bottom:8px;">Bill / Ship To</div>
        <div style="font-family:Georgia,serif;font-size:16px;font-weight:700;
                    color:#2C1018;margin-bottom:4px;">{order.get('customer_name','')}</div>
        <div style="font-family:Georgia,serif;font-size:13px;color:#6B5060;line-height:1.65;">
          {addr_line}
        </div>
        <div style="font-family:Georgia,serif;font-size:13px;color:#9B7080;margin-top:6px;
                    letter-spacing:0.06em;">{order.get('customer_phone','')}</div>
      </div>"""

    # Totals
    totals_rows = f"""
      <tr>
        <td style="font-family:Georgia,serif;font-size:12px;color:#9B7080;
                   letter-spacing:0.10em;text-transform:uppercase;padding:6px 0;">Subtotal</td>
        <td style="font-family:Georgia,serif;font-size:13px;color:#2C1018;
                   text-align:right;padding:6px 0;">{_fmt(subtotal)}</td>
      </tr>
      <tr>
        <td style="font-family:Georgia,serif;font-size:12px;color:#9B7080;
                   letter-spacing:0.10em;text-transform:uppercase;padding:6px 0;">Shipping</td>
        <td style="font-family:Georgia,serif;font-size:13px;color:#2C1018;
                   text-align:right;padding:6px 0;">{'FREE' if ship_charge == 0 else _fmt(ship_charge)}</td>
      </tr>"""
    if coupon > 0:
        totals_rows += f"""
      <tr>
        <td style="font-family:Georgia,serif;font-size:12px;color:#9B7080;
                   letter-spacing:0.10em;text-transform:uppercase;padding:6px 0;">Coupon Discount</td>
        <td style="font-family:Georgia,serif;font-size:13px;color:#2E8B57;font-weight:600;
                   text-align:right;padding:6px 0;">−{_fmt(coupon)}</td>
      </tr>"""

    return f"""
<div style="font-family:Georgia,serif;max-width:700px;margin:0 auto;
            background:#F6EEE1;">
  <!-- Accent bar -->
  <div style="height:5px;background:#6A012A;"></div>

  <!-- Header: slim burgundy bar -->
  <div style="background:#6A012A;padding:10px 24px;display:flex;align-items:center;
              justify-content:space-between;">
    <img src="https://www.onlytwam.com/assets/Twam-Logo_White.png" height="32" alt="{company}"
         style="display:block;" />
    <div style="text-align:right;">
      <div style="font-family:Georgia,serif;font-size:13px;font-weight:700;
                  color:#F6EEE1;letter-spacing:0.08em;line-height:1.3;">
        Order #{order_no}
      </div>
      <div style="font-family:Georgia,serif;font-size:11px;
                  color:rgba(246,238,225,0.70);letter-spacing:0.10em;line-height:1.3;">
        {order_date}
      </div>
    </div>
  </div>

  <!-- Body -->
  <div style="background:#fff;padding:32px 36px 0;">
    {addr_html}
    {_items_table(items)}

    <!-- Totals -->
    <div style="display:flex;justify-content:flex-end;padding:20px 0 0;">
      <table style="min-width:260px;border-collapse:collapse;">
        {totals_rows}
        <tr>
          <td colspan="2" style="padding:4px 0;"></td>
        </tr>
        <tr>
          <td colspan="2" style="padding:0;">
            <div style="display:flex;justify-content:space-between;align-items:center;
                        padding:14px 16px;
                        background:linear-gradient(135deg,#F6EEE1 0%,#fff 100%);
                        border:1.5px solid #EDE0CC;border-left:5px solid #6A012A;
                        border-radius:2px;margin-top:8px;">
              <span style="font-family:Georgia,serif;font-size:11px;letter-spacing:0.26em;
                           text-transform:uppercase;color:#9B7080;">Total</span>
              <span style="font-family:Georgia,serif;font-size:22px;font-weight:700;
                           color:#6A012A;letter-spacing:0.04em;">{_fmt(total)}</span>
            </div>
          </td>
        </tr>
      </table>
    </div>
  </div>

  <!-- Footer -->
  {_footer(company, support)}
</div>"""


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
            "transaction_id":  transaction_id[:50],
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


# ── SmartPing GET request helper ──────────────────────────────────────────────

def _smartping_request(api_url: str, params: dict):
    """
    Send a single GET request to SmartPing /fe/api/v1/send.
    Returns (http_status, raw_body, parsed_data).
    """
    import httpx
    import json

    try:
        print(f"[SMS DEBUG] URL   : {api_url}")
        print(f"[SMS DEBUG] PARAMS: {params}")

        resp = httpx.get(api_url, params=params, timeout=15)
        raw  = resp.text.strip()

        print(f"[SMS DEBUG] STATUS: {resp.status_code}")
        print(f"[SMS DEBUG] BODY  : {raw}")

        try:
            body = json.loads(raw)
        except Exception:
            body = {}

        state       = body.get("state", "")
        txn_id      = str(body.get("transactionId", ""))[:50]
        status_code = body.get("statusCode", resp.status_code)
        description = body.get("description", raw)
        ok          = state == "SUBMIT_ACCEPTED" or status_code == 200

        return resp.status_code, raw, {
            "statusCode":    status_code,
            "state":         "SUCCESS" if ok else "ERROR",
            "transactionId": txn_id,
            "description":   description,
        }

    except Exception as e:
        print(f"[SMS ERROR] {e}")
        return 0, str(e), {
            "statusCode":    0,
            "state":         "EXCEPTION",
            "transactionId": "",
            "description":   str(e),
        }


# ════════════════════════════════════════════════════════════════════════════════
# CORE SMS SENDER — fixed single-call flow
# ════════════════════════════════════════════════════════════════════════════════

def _send_sms(
    db: Session,
    phone: str,
    message_type: str,
    named_vars: dict,
    positional_order: list,
) -> bool:
    """
    Send SMS via SmartPing HTTP GET API.

    named_vars       : dict of key → value  e.g. {"otp": "835460"}
    positional_order : keys in DLT template order  e.g. ["otp"]
                       Position 1 → var1 → replaces {#var#} / ${var1} in template.

    Fixed flow (single API call):
      1. Fetch credential + template from DB (cached 5 min)
      2. Render text: replace {#var#} / ${var1} with actual values  ← KEY FIX
      3. Build GET params with rendered text + varN params
      4. GET /fe/api/v1/send  (called ONCE)
      5. Write SmsLog
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
        print(f"[SMS] No active template for '{message_type}'")
        return False

    dlt_content_id  = template["templateId"]
    dlt_peid        = template["dltId"]
    message_content = template["messageContent"]

    if not dlt_content_id:
        print(f"[SMS] Template '{message_type}' missing TemplateId")
        return False

    # ── Normalise phone → 12-digit 91XXXXXXXXXX ─────────────────────────────
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if len(digits) < 10:
        print(f"[SMS] Invalid phone: '{phone}'")
        return False
    recipient = "91" + digits[-10:]

    # ── Debug: show variable mapping ─────────────────────────────────────────
    print(f"[SMS] Sending {message_type} → {recipient}")
    print(f"[SMS] dltContentId={dlt_content_id} | dltPEID={dlt_peid}")
    for i, key in enumerate(positional_order, start=1):
        print(f"[SMS] var{i} ({key}) = {named_vars.get(key, '')}")

    # ── FIXED: Render text BEFORE building params ────────────────────────────
    # _render_template handles {#var#}, ${var1}, and {var1} formats
    # This must happen before the API call — the old code did this AFTER
    rendered_text = _render_template(message_content, positional_order, named_vars)

    print(f"[SMS] Rendered text: {rendered_text}")

    # ── Build SmartPing GET params ───────────────────────────────────────────
    params: dict = {
        "username":             cred["username"],
        "password":             cred["password"],
        "from":                 cred["senderId"],
        "to":                   recipient,
        "text":                 rendered_text,        # ← rendered, not raw template
        "dltContentId":         dlt_content_id,
        "dltPrincipalEntityId": dlt_peid,
        "unicode":              "true" if cred.get("unicode", "").lower() == "true" else "false",
    }

    # Add varN params for SmartPing DLT validation
    for i, key in enumerate(positional_order, start=1):
        params[f"var{i}"] = str(named_vars.get(key, "") or "")

    # ── Fire request ONCE ────────────────────────────────────────────────────
    http_status, raw, data = _smartping_request(cred["apiUrl"], params)

    print(f"[SMS] HTTP {http_status} | {raw[:300]}")

    ok          = data.get("state") == "SUCCESS"
    txn_id      = str(data.get("transactionId", ""))
    state_str   = data.get("state", "UNKNOWN")
    status_code = int(data.get("statusCode") or http_status or 0)
    error_desc  = "" if ok else (data.get("description") or raw[:200])

    # Log rendered text (what was actually sent)
    _log_sms(db, recipient, message_type, rendered_text,
             dlt_content_id, dlt_peid, txn_id, state_str, status_code, error_desc)

    if ok:
        print(f"[SMS] ✅ {message_type} → {recipient} txId={txn_id}")
    else:
        print(f"[SMS] ❌ {message_type} FAILED code={status_code} desc={error_desc}")

    return ok


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
# DB template : "Only TWAM Login OTP: {#var#}\nValid for 10 mins…"
# named_vars  : {"otp": <6-digit code>}
# positional  : var1 = otp
# Result      : "Only TWAM Login OTP: 835460\nValid for 10 mins…"
# ═══════════════════════════════════════════════════════════════════════════════
def send_otp_sms(db: Session, phone: str, otp: str) -> bool:
    if not phone:
        return False
    return _send_sms(
        db, phone, MT_OTP,
        named_vars       = {"otp": otp},
        positional_order = ["otp"],
    )


# ═══════════════════════════════════════════════════════════════════════════════
# WELCOME
# DB template : "Welcome to OnlyTWAM… Visit https://onlytwam.com/…"
# named_vars  : (none)
# positional  : (none)
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
# DB template : "Hi {#var#} Your Temporary Password is : {#var#}…"
# named_vars  : {"customer_name": name, "temp_password": password}
# positional  : var1=customer_name, var2=temp_password
# Result      : "Hi Eshwar Your Temporary Password is : Abc@123…"
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
# DB template : "Hi {#var#}, your order {#var#} is confirmed…"
# positional  : var1=customer_name, var2=order_number, var3=items,
#               var4=total_amount, var5=delivery_address
# ═══════════════════════════════════════════════════════════════════════════════
def send_order_placed_notification(db: Session, order_id: int) -> bool:
    order = _get_order_details(db, order_id)
    if not order:
        return False

    company = _cfg(db, "COMPANY_NAME", "Only TWAM")
    support = _cfg(db, "SUPPORT_EMAIL", "support@onlytwam.com")
    items   = _get_order_items(db, order_id)

    if order["customer_email"]:
        subtotal = sum(i["price"] * i["quantity"] for i in items)
        it = [{"name": i["name"], "quantity": i["quantity"], "price": i["price"],
               "size": i.get("size"), "color": i.get("color")} for i in items]
        support_it = [{"name": i["name"], "quantity": i["quantity"], "price": i["price"],
               "size": i.get("size"), "color": i.get("color"), "stock_id": i.get("stock_id")} for i in items]
        content = (
            greeting(order["customer_name"])
            + f'''<p style="margin:0 0 16px;">Your order has been confirmed.
              We'll get it packed and shipped as soon as possible!</p>'''
            + info_box([
                ("Order Number", f"#{order['order_number']}"),
                ("Order Date",   datetime.now().strftime("%d %b %Y")),
                ("Status",       "Confirmed ✓"),
            ], accent="#2E8B57")
            + items_table(it)
            + totals_block(subtotal,
                           order.get("delivery_charge") or 0,
                           order.get("total_amount") or 0,
                           order.get("coupon_amount") or 0)
            + section_divider()
            + info_box([
                ("Ship To", order.get("customer_name", "")),
                ("Address", ", ".join(filter(None, [
                    order.get("address_line"), order.get("locality"),
                    order.get("city"), order.get("pincode"),
                ]))),
                ("Phone",   order.get("customer_phone", "")),
            ])
        )
        body = twam_email(
            title   = f"Order Confirmed! #{order['order_number']}",
            content = content,
        )
        send_email(db, EmailCreate(
            name=order["customer_name"], email=order["customer_email"],
            subject=f"Order Confirmed! #{order['order_number']} — {company}", message=body,
        ))

        # Also notify customer support so the team has visibility on every
        # confirmed order without digging through the admin panel.
        # Support body includes the customer's email address explicitly so they
        # can identify who ordered without opening the admin panel.
        try:
            support_content = (
                greeting(order["customer_name"])
                + f'''<p style="margin:0 0 16px;">A new order has been confirmed.</p>'''
                + info_box([
                    ("Customer Name",  order["customer_name"]),
                    ("Customer Email", order["customer_email"]),
                    ("Customer Phone", order.get("customer_phone", "—")),
                    ("Order Number",   f"#{order['order_number']}"),
                    ("Order Date",     datetime.now().strftime("%d %b %Y")),
                    ("Status",         "Confirmed ✓"),
                ], accent="#2E8B57")
                + items_table(support_it)
                + totals_block(subtotal,
                               order.get("delivery_charge") or 0,
                               order.get("total_amount") or 0,
                               order.get("coupon_amount") or 0)
                + section_divider()
                + info_box([
                    ("Ship To", order.get("customer_name", "")),
                    ("Address", ", ".join(filter(None, [
                        order.get("address_line"), order.get("locality"),
                        order.get("city"), order.get("pincode"),
                    ]))),
                    ("Phone",   order.get("customer_phone", "")),
                ])
            )
            support_body = twam_email(
                title   = f"[Order Confirmed] #{order['order_number']} — {order['customer_name']}",
                content = support_content,
            )
            send_email(db, EmailCreate(
                name="Customer Support",
                email=support,
                subject=f"[Order Confirmed] #{order['order_number']} — {order['customer_name']} ({order['customer_email']})",
                message=support_body,
            ))
        except Exception as exc:
            print(f"[Notifications] Support copy of order-confirmed email failed for order {order_id}: {exc}")

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
# DB template : "Hi {#var#}, your order {#var#} of {#var#} is pending payment…"
# positional  : var1=customer_name, var2=order_number, var3=total_amount
# ═══════════════════════════════════════════════════════════════════════════════
def send_order_pending_notification(db: Session, order_id: int) -> bool:
    order = _get_order_details(db, order_id)
    if not order:
        return False
    company = _cfg(db, "COMPANY_NAME", "Only TWAM")
    support = _cfg(db, "SUPPORT_EMAIL", "support@onlytwam.com")

    if order["customer_email"]:
        content = (
            greeting(order["customer_name"])
            + '''<p style="margin:0 0 16px;">Your order has been received but
              payment confirmation is still pending. We'll notify you once confirmed.</p>'''
            + info_box([
                ("Order Number", f"#{order['order_number']}"),
                ("Amount",       _fmt(order["total_amount"])),
                ("Status",       "Payment Pending"),
            ], accent="#f5a623")
            + f'''<p style="margin:16px 0 0;font-size:13px;color:#9B7080;">
              Questions? <a href="mailto:{support}">{support}</a></p>'''
        )
        body = twam_email(
            title   = f"Payment Pending — Order #{order['order_number']}",
            content = content,
        )
        send_email(db, EmailCreate(
            name=order["customer_name"], email=order["customer_email"],
            subject=f"Order Payment Pending — #{order['order_number']}", message=body,
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
# DB template : "Hi {#var#}, payment for order {#var#} of {#var#} failed…"
# positional  : var1=customer_name, var2=order_number, var3=total_amount
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
        content = (
            greeting(order["customer_name"])
            + '''<p style="margin:0 0 16px;">Unfortunately, payment for your order
              could not be processed. Please retry with a different payment method.</p>'''
            + info_box([
                ("Order Number", f"#{order['order_number']}"),
                ("Amount",       _fmt(amount)),
                ("Status",       "Payment Failed"),
            ], accent="#c0392b")
            + '''<p style="margin:16px 0 0;font-size:13px;color:#9B7080;">
              If your account was debited, the amount will be refunded within 3–7 business days.</p>'''
            + f'''<p style="margin:8px 0 0;font-size:13px;color:#9B7080;">
              Need help? <a href="mailto:{support}">{support}</a></p>'''
        )
        body = twam_email(
            title   = f"Payment Failed — Order #{order['order_number']}",
            content = content,
        )
        send_email(db, EmailCreate(
            name=order["customer_name"], email=order["customer_email"],
            subject=f"Payment Failed — Order #{order['order_number']}", message=body,
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
# DB template : "A refund of {#var#} for order {#var#} is initiated on {#var#}…"
# positional  : var1=refund_amount, var2=order_number, var3=refund_date
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
# DB template : "Hi {#var#}, refund of {#var#} for order {#var#} via {#var#} on {#var#}…"
# positional  : var1=customer_name, var2=order_number, var3=refund_amount,
#               var4=payment_method, var5=refund_date
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
        content = (
            greeting(order["customer_name"])
            + f'''<p style="margin:0 0 16px;">Your refund for order
              <strong>#{order['order_number']}</strong> has been successfully processed.</p>'''
            + info_box([
                ("Refund Amount",  fmt_amount),
                ("Payment Method", payment_method),
                ("Refund Date",    fmt_date),
                ("Order Number",   f"#{order['order_number']}"),
            ], accent="#2E8B57")
            + '''<p style="margin:16px 0 0;font-size:13px;color:#9B7080;">
              Please allow 3–7 business days for the amount to reflect in your account.</p>'''
        )
        body = twam_email(
            title   = f"Refund Processed — Order #{order['order_number']}",
            content = content,
        )
        send_email(db, EmailCreate(
            name=order["customer_name"], email=order["customer_email"],
            subject=f"Refund Processed — Order #{order['order_number']}", message=body,
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
# DB template : "Hi {#var#}, your order {#var#} is shipped. Track: {#var#}…"
# positional  : var1=customer_name, var2=order_number, var3=tracking_id
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
        rows = [
            ("Order Number", f"#{order['order_number']}"),
            ("Tracking ID",  order["tracking_id"] or "Processing…"),
        ]
        content = (
            greeting(order["customer_name"])
            + '''<p style="margin:0 0 16px;">Great news — your order is on its way!</p>'''
            + info_box(rows)
            + (cta_button("Track Your Package", tracking_url) if tracking_url else "")
        )
        body = twam_email(
            title   = f"Your Order is On Its Way! #{order['order_number']}",
            content = content,
        )
        send_email(db, EmailCreate(
            name=order["customer_name"], email=order["customer_email"],
            subject=f"Your Order is On Its Way! #{order['order_number']}", message=body,
        ))

        # Notify customer support — separate body that explicitly shows the
        # customer's name, email and phone so support can identify the user.
        try:
            items = _get_order_items(db, order_id)
            support_it = [{"name": i["name"], "quantity": i["quantity"], "price": i["price"],
                   "size": i.get("size"), "color": i.get("color"), "stock_id": i.get("stock_id")} for i in items]
            support_shipped_content = (
                greeting("Customer Support Team")
                + f'''<p style="margin:0 0 16px;">A shipment has been created for the following order.</p>'''
                + info_box([
                    ("Customer Name",  order["customer_name"]),
                    ("Customer Email", order["customer_email"] or "—"),
                    ("Customer Phone", order.get("customer_phone", "—")),
                    ("Order Number",   f"#{order['order_number']}"),
                    ("Tracking ID",    order["tracking_id"] or "Processing…"),
                    ("Tracking URL",   tracking_url or "—"),
                ], accent="#2E8B57")
                + items_table(support_it)
                + (cta_button("Track Package", tracking_url) if tracking_url else "")
            )
            support_shipped_body = twam_email(
                title   = f"[Order Shipped] #{order['order_number']} — {order['customer_name']}",
                content = support_shipped_content,
            )
            send_email(db, EmailCreate(
                name="Customer Support",
                email=support,
                subject=f"[Order Shipped] #{order['order_number']} — {order['customer_name']} ({order['customer_email'] or 'no email'}) — AWB {order['tracking_id'] or 'Processing'}",
                message=support_shipped_body,
            ))
        except Exception as exc:
            print(f"[Notifications] Support copy of shipped email failed for order {order_id}: {exc}")

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
# DB template : "Hi {#var#}, your order {#var#} is out for delivery today…"
# positional  : var1=customer_name, var2=order_number
# ═══════════════════════════════════════════════════════════════════════════════
def send_out_for_delivery_notification(db: Session, order_id: int) -> bool:
    order = _get_order_details(db, order_id)
    if not order:
        return False
    company = _cfg(db, "COMPANY_NAME", "Only TWAM")
    support = _cfg(db, "SUPPORT_EMAIL", "support@onlytwam.com")

    if order["customer_email"]:
        content = (
            greeting(order["customer_name"])
            + f'''<p style="margin:0 0 16px;">Your order <strong>#{order['order_number']}</strong>
              is out for delivery today! Please ensure someone is available to receive the package.</p>'''
            + info_box([
                ("Order Number", f"#{order['order_number']}"),
                ("Tracking ID",  order["tracking_id"] or "N/A"),
                ("Status",       "Out for Delivery 🚚"),
            ], accent="#2E8B57")
        )
        body = twam_email(
            title   = f"Arriving Today! Order #{order['order_number']}",
            content = content,
        )
        send_email(db, EmailCreate(
            name=order["customer_name"], email=order["customer_email"],
            subject=f"Arriving Today! — Order #{order['order_number']}", message=body,
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
# DB template : "Hi {#var#}, your order {#var#} has been delivered…"
# positional  : var1=customer_name, var2=order_number
# ═══════════════════════════════════════════════════════════════════════════════
def send_delivery_confirmation(
    db: Session, order_id: int, pod_image_url: Optional[str] = None
) -> bool:
    order = _get_order_details(db, order_id)
    if not order:
        return False
    company = _cfg(db, "COMPANY_NAME", "Only TWAM")
    support = _cfg(db, "SUPPORT_EMAIL", "support@onlytwam.com")
    website = _cfg(db, "WEBSITE_URL", "https://www.onlytwam.com")

    if order["customer_email"]:
        content = (
            greeting(order["customer_name"])
            + f'''<p style="margin:0 0 16px;">Your order
              <strong>#{order['order_number']}</strong> has been delivered!
              We hope you love your new purchase. 🎉</p>'''
            + info_box([
                ("Order Number", f"#{order['order_number']}"),
                ("Status",       "Delivered ✓"),
            ], accent="#2E8B57")
            + cta_button("Leave a Review", f"{website}/account/orders-list")
            + '''<p style="margin:16px 0 0;font-size:12px;color:#9B7080;text-align:center;">
              Need to return? Visit "My Orders" within 7 days.</p>'''
        )
        body = twam_email(
            title   = f"Delivered! Order #{order['order_number']}",
            content = content,
        )
        send_email(db, EmailCreate(
            name=order["customer_name"], email=order["customer_email"],
            subject=f"Delivered! Order #{order['order_number']} — {company}", message=body,
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
# DB template : "Hi {#var#}, your order {#var#} of {#var#} has been cancelled…"
# positional  : var1=customer_name, var2=order_number, var3=total_amount,
#               var4=cancelled_by, var5=reason
# ═══════════════════════════════════════════════════════════════════════════════
class CancelledBy(str, Enum):
    CUSTOMER  = "customer"
    ADMIN     = "admin"
    TECHNICAL = "technical"


def send_order_cancelled_notification(
    db: Session,
    order_id: int,
    reason: Optional[str] = None,
    cancelled_by: CancelledBy = CancelledBy.CUSTOMER,
) -> bool:
    order = _get_order_details(db, order_id)
    if not order:
        return False

    company = _cfg(db, "COMPANY_NAME", "Only TWAM")
    support = _cfg(db, "SUPPORT_EMAIL", "support@onlytwam.com")

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

    else:  # CancelledBy.CUSTOMER
        email_heading  = "Order Cancelled"
        email_intro    = (
            f"Your order <strong>#{order['order_number']}</strong> has been cancelled"
            f"{f' — {reason}' if reason else ''}."
        )
        whatsapp_intro = (
            f"Hi {order['customer_name']}, your order #{order['order_number']} has been cancelled."
        )
        refund_note    = "If you paid online, your refund will be processed within 5-7 business days."

    if order["customer_email"]:
        rows = [
            ("Order Number", f"#{order['order_number']}"),
            ("Amount",       _fmt(order["total_amount"])),
            ("Status",       "Cancelled"),
        ]
        if reason:
            rows.append(("Reason", reason))
        content = (
            greeting(order["customer_name"])
            + f'''<p style="margin:0 0 16px;">{email_intro}</p>'''
            + info_box(rows, accent="#c0392b")
            + f'''<p style="margin:16px 0 0;font-size:13px;color:#9B7080;">{refund_note}</p>'''
        )
        body = twam_email(
            title   = f"{email_heading} — #{order['order_number']}",
            content = content,
        )
        send_email(db, EmailCreate(
            name=order["customer_name"],
            email=order["customer_email"],
            subject=f"{email_heading} — #{order['order_number']}",
            message=body,
        ))

        try:
            items = _get_order_items(db, order_id)
            support_it = [{"name": i["name"], "quantity": i["quantity"], "price": i["price"],
                   "size": i.get("size"), "color": i.get("color"), "stock_id": i.get("stock_id")} for i in items]
            
            support_cancelled_content = (
                greeting("Customer Support Team")
                + f'''<p style="margin:0 0 16px;">An order has been cancelled by {cancelled_by.value}.</p>'''
                + info_box([
                    ("Customer Name",  order["customer_name"]),
                    ("Customer Email", order["customer_email"] or "—"),
                    ("Customer Phone", order.get("customer_phone", "—")),
                    ("Order Number",   f"#{order['order_number']}"),
                    ("Reason",         reason or "None"),
                ], accent="#c0392b")
                + items_table(support_it)
            )
            support_cancelled_body = twam_email(
                title   = f"[Order Cancelled] #{order['order_number']} — {order['customer_name']}",
                content = support_cancelled_content,
            )
            send_email(db, EmailCreate(
                name="Customer Support",
                email=support,
                subject=f"[Order Cancelled] #{order['order_number']} — {order['customer_name']} ({order['customer_email'] or 'no email'})",
                message=support_cancelled_body,
            ))
        except Exception as exc:
            print(f"[Notifications] Support copy of cancelled email failed for order {order_id}: {exc}")

    if order["customer_phone"]:
        _notify(
            db, order, MT_ORDER_CANCELLED,
            named_vars={
                "customer_name": order["customer_name"],
                "order_number":  order["order_number"],
                "total_amount":  _fmt(order["total_amount"]),
                "cancelled_by":  cancelled_by.value,
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
# DB template : "Hi {#var#}, your return for order {#var#} has been received…"
# positional  : var1=customer_name, var2=order_number
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
        rows = [("Order Number", f"#{order['order_number']}")]
        if return_reason:
            rows.append(("Reason", return_reason))
        content = (
            greeting(order["customer_name"])
            + f'''<p style="margin:0 0 16px;">We've received your return request for
              order <strong>#{order['order_number']}</strong>.</p>'''
            + info_box(rows, accent="#f5a623")
            + '''<p style="margin:16px 0 4px;font-size:13px;"><strong>What happens next?</strong></p>
              <ol style="margin:0;padding-left:20px;font-size:13px;color:#6B5060;line-height:1.8;">
                <li>Our team will review your request</li>
                <li>You'll receive pickup details within 24–48 hours</li>
                <li>Refund will be processed after we receive the item</li>
              </ol>'''
            + f'''<p style="margin:16px 0 0;font-size:13px;color:#9B7080;">
              Questions? <a href="mailto:{support}">{support}</a></p>'''
        )
        body = twam_email(
            title   = f"Return Request Received — #{order['order_number']}",
            content = content,
        )
        send_email(db, EmailCreate(
            name=order["customer_name"], email=order["customer_email"],
            subject=f"Return Request Received — #{order['order_number']}", message=body,
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