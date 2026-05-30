"""
app/payment/payg_client.py
──────────────────────────
PayG payment gateway API client.

All credentials are fetched from AppSettings (DB) at call-time so they
can be updated through the admin panel without restarting the server.
Zero hardcoded secrets in this file.

PayG integration flow:
  1. call create_payg_order()  → returns ProcessingUrl
  2. redirect user's browser to ProcessingUrl
  3. user pays on PayG-hosted page
  4. PayG redirects browser to our RedirectUrl (GET or POST)
  5. /api/Payment/Callback verifies hash, updates order, redirects to frontend

PayG response codes (PaymentResponseCode):
  0 – Initiated / pending
  1 – Approved  ✓
  2 – Declined  ✗
  4 – Pending (awaiting bank confirmation)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import uuid
from datetime import datetime
from typing import Optional

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore


# ── API base URLs ──────────────────────────────────────────────────────────

_UAT_CREATE_URL  = "https://uatapiv2.payg.in/payment/api/order/create"
_LIVE_CREATE_URL = "https://apiv2.payg.in/payment/api/order/create"
_UAT_DETAIL_URL  = "https://uatapiv2.payg.in/payment/api/order/detail"
_LIVE_DETAIL_URL = "https://apiv2.payg.in/payment/api/order/detail"


# ── Credential helpers ─────────────────────────────────────────────────────

def _cfg(key: str, default: str = "") -> str:
    """Read a setting from the in-process AppSettings cache."""
    from app.shared.app_settings import get_setting
    return get_setting(key, default)


def _get_create_url() -> str:
    return _LIVE_CREATE_URL if _cfg("payg_mode", "uat").lower() == "live" else _UAT_CREATE_URL


def _get_detail_url() -> str:
    return _LIVE_DETAIL_URL if _cfg("payg_mode", "uat").lower() == "live" else _UAT_DETAIL_URL


def _sanitize_phone(phone) -> str:
    """
    PayG requires exactly a 10-digit Indian mobile number (no +91, no spaces).

    FIX: Handles all messy inputs:
      - None / empty string  → falls back to merchant support phone
      - Accidental tuple     → unwraps first element (trailing-comma bug in repository)
      - "+91 7036534449"     → "7036534449"
      - "91 9876543210"      → "9876543210"
      - "07036534449"        → "7036534449"
      - Already clean 10-dig → returned as-is
    """
    # Unwrap accidental tuple from repository trailing-comma bug
    if isinstance(phone, (tuple, list)):
        phone = phone[0] if phone else ""

    p = re.sub(r"[^\d]", "", str(phone or ""))   # strip everything except digits

    if p.startswith("91") and len(p) == 12:       # strip STD +91
        p = p[2:]
    if p.startswith("0") and len(p) == 11:        # strip leading 0
        p = p[1:]

    print(f"[PayG] _sanitize_phone({phone!r}) → '{p}'")

    if len(p) == 10 and p[0] in "6789":
        return p

    # Fallback: use SUPPORT_PHONE from AppSettings (a real registered number)
    support = re.sub(r"[^\d]", "", _cfg("SUPPORT_PHONE", "7036534449"))
    if support.startswith("91") and len(support) == 12:
        support = support[2:]
    if len(support) == 10 and support[0] in "6789":
        print(f"[PayG] Phone fallback to support phone: '{support}'")
        return support

    return "7036534449"   # absolute last resort — the merchant support number


def _strip_0x(value: str) -> str:
    """
    PayG portal displays hex keys with a '0x' prefix (e.g. '0xcf7407...').
    The actual key value is the raw hex WITHOUT the prefix.
    Strip it defensively so pasting directly from the portal never breaks auth.
    """
    return value[2:] if value.startswith("0x") or value.startswith("0X") else value


def _build_auth_header() -> str:
    """
    PayG requires:
      Authorization: Basic base64(AuthKey:AuthToken:MerchantKeyId:MID)

    Format confirmed from PayG documentation (base64encode.org screenshot):
      MerchantAuthenticationKey:MerchantAuthenticationToken:M:MID
      i.e. AuthKey:AuthToken:MerchantKeyId:MID

    Keys are stored with or without 0x prefix depending on how they were entered.
    Always strip 0x before encoding — PayG validates the raw hex values.
    """
    merchant_key_id = _cfg("payg_merchant_key_id")
    auth_key        = _strip_0x(_cfg("payg_auth_key"))
    auth_token      = _strip_0x(_cfg("payg_auth_token"))
    mid             = _cfg("payg_mid")
    raw             = f"{auth_key}:{auth_token}:{merchant_key_id}:{mid}"
    encoded         = base64.b64encode(raw.encode()).decode()
    return f"Basic {encoded}"


# ── Hash computation ───────────────────────────────────────────────────────

def compute_hash(mid: str, unique_request_id: str, order_amount: str) -> str:
    """
    PayG hash = HMAC-SHA256(SecureHashKey, "MID|UniqueRequestId|OrderAmount")
    Returns uppercase hex string.

    BUG FIX: order_amount must always have exactly 2 decimal places ("100.50"
    not "100.5"). Use f"{float(amount):.2f}" when calling this function.

    BUG FIX: Strip 0x portal-display prefix from SecureHashKey before use.
    PayG computes the HMAC with the raw hex key — including 0x causes mismatch.
    """
    secret  = _strip_0x(_cfg("payg_secure_hash", ""))
    message = f"{mid}|{unique_request_id}|{order_amount}"
    sig = hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest().upper()
    return sig


def verify_callback_hash(response_data: dict) -> bool:
    """
    Verify that the PayG callback payload was signed by PayG using our
    SecureHashKey.  Returns False if the HashData field is missing or wrong.
    In UAT dev mode an empty hash is treated as 'no verification' by the
    caller — this function still validates mathematically if a hash is present.
    """
    received_hash = response_data.get("HashData", "")
    if not received_hash:
        return False
    mid           = response_data.get("MID", "")
    unique_req_id = response_data.get("UniqueRequestId", "")
    order_amount  = str(response_data.get("OrderAmount", ""))
    expected = compute_hash(mid, unique_req_id, order_amount)
    return hmac.compare_digest(expected, received_hash.upper())


# ── Order creation ─────────────────────────────────────────────────────────

def create_payg_order(
    *,
    order_id: int,
    order_number: str,
    amount: float,
    customer_name: str,
    customer_email: str,
    customer_phone,          # str | tuple | None — sanitized internally
    billing_address: str,
    billing_city: str,
    billing_state: str,
    billing_zip: str,
    redirect_url: str,
    shipping_state: Optional[str] = None,
) -> dict:
    """
    Call PayG /payment/api/order/create and return the full response dict.
    On success the dict contains 'ProcessingUrl' — redirect the user there.
    Also includes '_unique_request_id' for tracking.
    Raises RuntimeError on HTTP or PayG-level failure.
    """
    mid               = _cfg("payg_mid")
    unique_request_id = f"ORD{order_id}-{uuid.uuid4().hex[:8].upper()}"

    # BUG FIX: Always format amount to exactly 2 decimal places.
    # str(round(100.5, 2)) → '100.5' which breaks the HMAC hash verification.
    # f"{100.5:.2f}" → '100.50' which matches what PayG expects.
    amount_str  = f"{float(amount):.2f}"
    now_str     = datetime.now().strftime("%m%d%Y")   # MMDDYYYY
    hash_data   = compute_hash(mid, unique_request_id, amount_str)
    clean_phone = _sanitize_phone(customer_phone)

    first, *rest = (customer_name or "Customer").split(" ", 1)
    last = rest[0] if rest else ""

    # ProductData MUST be a JSON string, not a dict object.
    product_data_str = f"{{'PaymentReason': 'Order {order_number}'}}"

    # ── FIX: PayG UAT does not append params to RedirectUrl on browser redirect.
    # Embed order_id (oid) and unique_request_id (rid) into the redirect URL
    # so the callback can always identify the order and call the Detail API.
    sep = "&" if "?" in redirect_url else "?"
    redirect_url_with_ids = f"{redirect_url}{sep}oid={order_id}&rid={unique_request_id}"

    payload = {
        "MID": mid,
        "UniqueRequestId": unique_request_id,
        "UserDefinedData": {
            "UserDefined1": str(order_id),
        },
        "ProductData": product_data_str,
        "RequestDateTime": now_str,
        "RedirectUrl": redirect_url_with_ids,
        "TransactionData": {
            "AcceptedPaymentTypes": "",
            "PaymentType": "",
            "SurchargeType": "",
            "SurchargeValue": "",
            "RefTransactionId": "",
            "IndustrySpecificationCode": "",
            "PartialPaymentOption": "",
        },
        "OrderAmount": amount_str,
        "OrderType": "",
        "OrderAmountData": {
            "AmountTypeDesc": "3",
            "Amount": amount_str,
        },
        "CustomerData": {
            "CustomerId": str(order_id),
            "CustomerNotes": f"Order {order_number}",
            "FirstName": first,
            "LastName": last,
            "MobileNo": clean_phone,
            "Email": customer_email or "",
            "EmailReceipt": "true",
            "BillingAddress": billing_address or "",
            "BillingCity": billing_city or "",
            "BillingState": billing_state or "",
            "BillingCountry": "India",
            "BillingZipCode": billing_zip or "",
            "ShippingFirstName": first,
            "ShippingLastName": last,
            "ShippingAddress": billing_address or "",
            "ShippingCity": billing_city or "",
            "ShippingState": shipping_state or billing_state or "",
            "ShippingCountry": "India",
            "ShippingZipCode": billing_zip or "",
            "ShippingMobileNo": clean_phone,
        },
        "IntegrationData": {
            "UserName": first,
            "Source": "3213",
            "IntegrationType": "",
            "HashData": hash_data,
            "PlatformId": "1",
        },
    }

    headers = {
        "Authorization": _build_auth_header(),
        "Content-Type": "application/json",
    }

    create_url = _get_create_url()
    print(f"[PayG] Calling {create_url}")
    print(f"[PayG] MID={mid}, MerchantKeyId={_cfg('payg_merchant_key_id')}, "
          f"Phone={clean_phone}, RedirectUrl={redirect_url}")

    try:
        resp = httpx.post(
            create_url,
            json=payload,
            headers=headers,
            timeout=15.0,
        )
        print(f"[PayG] HTTP {resp.status_code} from {create_url}")
        print(f"[PayG] Response body: {resp.text[:1000]}")
        resp.raise_for_status()
        data = resp.json()
    except httpx.ConnectTimeout as exc:
        raise RuntimeError(
            "PayG gateway is unreachable (connect timeout). "
            "Possible causes:\n"
            "  1. Your network/ISP is blocking uatapiv2.payg.in — try a mobile hotspot.\n"
            "  2. PayG UAT server is temporarily down — check https://uat.payg.in\n"
            "  3. payg_redirect_url in AppSettings is still the placeholder — update it "
            "to your current ngrok URL, e.g. https://your-id.ngrok-free.dev/api/Payment/Callback"
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"PayG HTTP error {exc.response.status_code}: {exc.response.text}"
        ) from exc
    except Exception as exc:
        raise RuntimeError(f"PayG request failed: {exc}") from exc

    # Validate that PayG actually returned a payment URL.
    # PayG can return HTTP 200 with an error payload — we must detect this.
    processing_url = data.get("ProcessingUrl") or data.get("PaymentProcessUrl")
    if not processing_url:
        error_msg = (
            data.get("message")
            or data.get("Message")
            or data.get("error")
            or data.get("Error")
            or data.get("ResponseMessage")
            or data.get("responseMessage")
            or "No ProcessingUrl returned"
        )
        raise RuntimeError(
            f"PayG order creation failed — {error_msg}. "
            f"Full PayG response: {json.dumps(data)}"
        )

    data["_unique_request_id"] = unique_request_id
    return data


# ── Order status / detail ──────────────────────────────────────────────────

def get_payg_order_detail(
    order_key_id: Optional[str] = None,
    unique_request_id: Optional[str] = None,
    mid: Optional[str] = None,
) -> dict:
    """
    Fetch PayG /payment/api/order/detail to get live payment status.
    Pass either order_key_id OR unique_request_id.

    PaymentResponseCode meanings:
      0 – Initiated
      1 – Approved  ✓
      2 – Declined  ✗
      4 – Pending

    RETRY LOGIC: PayG sometimes redirects the browser to the callback URL
    *before* the payment is fully committed on their end (race condition).
    When the Detail API is called immediately after the redirect, PayG may
    return code=0 (Initiated) or a response with no PaymentResponseCode at all.
    We retry up to 3 times with a 2-second delay to give PayG time to settle.
    A resolved code (1 or 2) exits early; code=0/4/absent retries.

    IMPORTANT — PayG Detail API does NOT require an Authorization header.
    The official doc shows only { OrderKeyId/UniqueRequestId, MID } in the body.
    Sending an Authorization header does not break it, but the Detail API is
    IP-allowlisted: if the server's outbound IP is not registered with PayG,
    every call returns HTTP 403 "Host not in allowlist".  In that case there
    is NOTHING we can do programmatically — the merchant must log into the PayG
    portal and add the server's IP to the allowlist.
    """
    import time

    _mid = mid or _cfg("payg_mid")
    ref = order_key_id or unique_request_id or "?"

    body: dict = {"MID": _mid}
    if order_key_id:
        body["OrderKeyId"] = order_key_id
    elif unique_request_id:
        body["UniqueRequestId"] = unique_request_id
    else:
        raise ValueError("Provide either order_key_id or unique_request_id")

    # PayG Detail API doc shows only Content-Type — no Authorization header.
    # We include it anyway (some PayG accounts require it, some don't),
    # but 403 "Host not in allowlist" means the server IP is blocked, not
    # that auth is wrong.
    headers = {
        "Authorization": _build_auth_header(),
        "Content-Type": "application/json",
    }

    detail_url = _get_detail_url()
    MAX_ATTEMPTS = 3
    RETRY_DELAY  = 2.0  # seconds between attempts

    last_exc: Exception | None = None
    last_data: dict = {}

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            print(
                f"[PayG] Detail API attempt {attempt}/{MAX_ATTEMPTS} | "
                f"ref={ref!r} | url={detail_url}"
            )
            resp = httpx.post(
                detail_url,
                json=body,
                headers=headers,
                timeout=15.0,
            )
            print(
                f"[PayG] Detail API HTTP {resp.status_code} | "
                f"body={resp.text[:500]!r}"
            )

            # ── 403 = "Host not in allowlist" ────────────────────────────────
            # PayG IP-allowlists the Detail API endpoint. This is NOT an auth
            # error and NOT a transient error — retrying won't help.
            # The merchant MUST add this server's public IP to the PayG portal:
            #   UAT portal:  https://uat.payg.in/merchant/#/login
            #   Live portal: https://payg.in/merchant/#/login
            #   → Settings → IP Whitelist → Add server IP
            if resp.status_code == 403:
                body_text = resp.text.strip()
                print(
                    f"[PayG] Detail API ❌ 403 FORBIDDEN — '{body_text}'\n"
                    f"  This means your server's public IP is NOT whitelisted in the PayG portal.\n"
                    f"  ACTION REQUIRED:\n"
                    f"    1. Find your server's public IP (run: curl ifconfig.me)\n"
                    f"    2. Log into PayG portal → Settings → IP Whitelist → Add IP\n"
                    f"  Until the IP is whitelisted, the Detail API will always fail\n"
                    f"  and every payment callback will show as 'declined' even when\n"
                    f"  the user's payment was successful."
                )
                # Don't retry — 403 is deterministic
                raise RuntimeError(
                    f"PayG Detail API blocked: {body_text}. "
                    f"Add this server's IP to the PayG portal IP whitelist."
                )

            resp.raise_for_status()
            data = resp.json()
            code = data.get("PaymentResponseCode")
            print(
                f"[PayG] Detail API attempt {attempt} → "
                f"PaymentResponseCode={code!r} | OrderStatus={data.get('OrderStatus')!r}"
            )
            # A resolved code (1=Approved, 2=Declined) means we're done — return immediately.
            # Code 0 (Initiated) or 4 (Pending) or absent → retry after delay if we have attempts left.
            if code in (1, 2):
                return data
            last_data = data
        except httpx.HTTPStatusError as exc:
            last_exc = RuntimeError(
                f"PayG detail HTTP error {exc.response.status_code}: {exc.response.text}"
            )
            print(f"[PayG] Detail API attempt {attempt} ❌ HTTP error: {exc.response.status_code}")
        except RuntimeError:
            raise  # re-raise our own errors (e.g. the 403 above) without retry
        except Exception as exc:
            last_exc = RuntimeError(f"PayG detail request failed: {exc}")
            print(f"[PayG] Detail API attempt {attempt} ❌ Exception: {exc}")

        if attempt < MAX_ATTEMPTS:
            print(f"[PayG] Detail API — waiting {RETRY_DELAY}s before retry...")
            time.sleep(RETRY_DELAY)

    # All attempts exhausted — return whatever we got last (may have code=0/4/absent),
    # or raise if we got nothing but exceptions.
    if last_data:
        print(
            f"[PayG] Detail API all {MAX_ATTEMPTS} attempts done — "
            f"returning last data (code={last_data.get('PaymentResponseCode')!r})"
        )
        return last_data

    if last_exc:
        raise last_exc

    raise RuntimeError(f"PayG detail API: no response after {MAX_ATTEMPTS} attempts")


# ── Response code → order state mapping ───────────────────────────────────

def payment_code_to_order_state(code: int) -> str:
    """Map PayG PaymentResponseCode to our internal order state string."""
    return {
        0:  "AwaitingPayment",   # Initiated — not settled yet
        1:  "Confirmed",         # Approved ✓
        2:  "Payment Failed",    # Declined ✗ (bank said no)
        4:  "Payment Pending",   # Awaiting bank confirmation
        -1: "AwaitingPayment",   # Unknown — Detail API failed; do NOT mark as failed
    }.get(code, "Payment Failed")