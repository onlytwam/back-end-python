"""
app/payment/payg_client.py
──────────────────────────
BUG FIXES IN THIS VERSION:

1. Detail API auth format — FIXED
   Create API  → Basic base64(auth_key:auth_token:merchant_key_id:mid)
   Detail API  → Basic base64(auth_key:auth_token)   ← shorter, no mid/key_id

   _build_detail_auth_header() is active and called inside get_payg_order_detail().

2. _parse_response_code() — safely coerces None/null → -1 (UNKNOWN sentinel)
3. payment_code_to_order_state() — maps only real PayG codes (1, 2, 4) + -1
4. 403 on Detail API exits immediately (no point retrying IP-blocked calls)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from datetime import datetime
from typing import Any, Optional

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore


# ── API base URLs ──────────────────────────────────────────────────────────

_UAT_CREATE_URL  = "https://uatapiv2.payg.in/payment/api/order/create"
_LIVE_CREATE_URL = "https://apiv2.payg.in/payment/api/order/create"
_UAT_DETAIL_URL  = "https://uatapiv2.payg.in/payment/api/order/detail"
_LIVE_DETAIL_URL = "https://apiv2.payg.in/payment/api/order/detail"

# UPI Intent — separate endpoint for mobile UPI app redirection
_UAT_INTENT_URL  = "https://uatapiv2.payg.in/payment/api/order/CreateIntent"
_LIVE_INTENT_URL = "https://apiv2.payg.in/payment/api/order/CreateIntent"


# ── Response code normalisation ────────────────────────────────────────────

def _parse_response_code(raw) -> int:
    """
    Safely convert whatever PayG sends for PaymentResponseCode to an int.

    PayG official codes:
      1  → Approved
      2  → Declined / Failed
      4  → Pending

    None / absent / "" / 0 / any non-numeric → -1 (UNKNOWN sentinel)
    -1 means "we don't know yet" — never mark order as failed for this.
    """
    if raw is None or raw == "" or raw == "-1":
        return -1
    try:
        code = int(raw)
        if code not in (1, 2, 4):
            print(f"[PayG] ⚠️  Unexpected PaymentResponseCode={code!r} — treating as -1 (UNKNOWN)")
            return -1
        return code
    except (ValueError, TypeError):
        print(f"[PayG] ⚠️  Non-numeric PaymentResponseCode={raw!r} — treating as -1 (UNKNOWN)")
        return -1


# ── JSON safety ────────────────────────────────────────────────────────────

class _SafeEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, (set, frozenset)):
            coerced = sorted(o, key=str)
            print(f"[PayG] ⚠️  Coerced {type(o).__name__} → list: {coerced!r}.")
            return coerced
        if isinstance(o, (bytes, bytearray)):
            return o.hex()
        print(f"[PayG] ⚠️  Unknown non-serializable type {type(o).__name__!r}: {o!r} → str()")
        return str(o)


def _safe_json_post(
    url: str,
    payload: dict,
    headers: dict,
    timeout: float = 15.0,
) -> "httpx.Response":
    body = json.dumps(payload, cls=_SafeEncoder, ensure_ascii=False).encode("utf-8")
    return httpx.post(
        url,
        content=body,
        headers={**headers, "Content-Type": "application/json"},
        timeout=timeout,
    )


# ── Credential helpers ─────────────────────────────────────────────────────

def _cfg(key: str, default: str = "") -> str:
    from app.shared.app_settings import get_setting
    value = get_setting(key, default)
    if not isinstance(value, str):
        print(f"[PayG] ⚠️  get_setting({key!r}) returned {type(value).__name__!r} — coercing.")
        return str(value) if value is not None else default
    return value


def _is_live() -> bool:
    return _cfg("payg_mode", "uat").strip().lower() == "live"


def _get_create_url() -> str:
    return _LIVE_CREATE_URL if _is_live() else _UAT_CREATE_URL


def _get_intent_url() -> str:
    return _LIVE_INTENT_URL if _is_live() else _UAT_INTENT_URL


def _get_detail_url() -> str:
    return _LIVE_DETAIL_URL if _is_live() else _UAT_DETAIL_URL


def _strip_0x(value: str) -> str:
    v = (value or "").strip()
    return v[2:] if v.lower().startswith("0x") else v


def _sanitize_phone(phone: Any) -> str:
    if isinstance(phone, (set, frozenset)):
        phone = next(iter(phone), "")
    if isinstance(phone, (tuple, list)):
        phone = phone[0] if phone else ""

    digits = re.sub(r"[^\d]", "", str(phone or ""))

    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]

    print(f"[PayG] _sanitize_phone({phone!r}) → '{digits}'")

    if len(digits) == 10 and digits[0] in "6789":
        return digits

    support = re.sub(r"[^\d]", "", _cfg("SUPPORT_PHONE", "7036534449"))
    if support.startswith("91") and len(support) == 12:
        support = support[2:]
    if len(support) == 10 and support[0] in "6789":
        print(f"[PayG] Phone fallback → support phone: '{support}'")
        return support

    return "7036534449"


def _build_auth_header() -> str:
    """
    Auth header for CREATE API.
    Format: Basic base64(auth_key:auth_token:merchant_key_id:mid)
    """
    auth_key     = _cfg("payg_auth_key")
    auth_token   = _cfg("payg_auth_token")
    merchant_key = _cfg("payg_merchant_key_id")
    mid          = _cfg("payg_mid")

    print(f"[PayG] Create auth: key='{auth_key[:4]}...' token='{auth_token[:4]}...' mid='{mid}'")

    raw = f"{auth_key}:{auth_token}:{merchant_key}:{mid}"
    encoded = base64.b64encode(raw.encode("utf-8")).decode("utf-8")
    return f"Basic {encoded}"


def _build_upi_intent_auth_header() -> str:
    """
    Auth header for UPI Intent CreateIntent API.
    Format: Basic base64(auth_key:auth_token:M:mid)
    NOTE: literal capital "M" replaces merchant_key_id — confirmed by PayG support.
    """
    auth_key   = _cfg("payg_auth_key")
    auth_token = _cfg("payg_auth_token")
    mid        = _cfg("payg_mid")

    print(f"[PayG] Intent auth: key='{auth_key[:4]}...' token='{auth_token[:4]}...' mid='{mid}' (M literal)")

    raw = f"{auth_key}:{auth_token}:M:{mid}"
    encoded = base64.b64encode(raw.encode("utf-8")).decode("utf-8")
    return f"Basic {encoded}"


def _build_detail_auth_header() -> str:
    """
    Auth header for DETAIL API.
    Format: Basic base64(auth_key:auth_token:M:mid)
    CONFIRMED by PayG support — same literal "M" format as CreateIntent,
    NOT the merchantKeyId used by the standard Create API.
    """
    auth_key   = _cfg("payg_auth_key")
    auth_token = _cfg("payg_auth_token")
    mid        = _cfg("payg_mid")

    print(f"[PayG] Detail auth: key='{auth_key[:4]}...' token='{auth_token[:4]}...' mid='{mid}' (M literal)")

    raw     = f"{auth_key}:{auth_token}:M:{mid}"
    encoded = base64.b64encode(raw.encode("utf-8")).decode("utf-8")
    return f"Basic {encoded}"


# ── Hash ───────────────────────────────────────────────────────────────────

def compute_hash(mid: str, unique_request_id: str, order_amount: str) -> str:
    secret = _strip_0x(_cfg("payg_secure_hash", ""))
    if not secret:
        print("[PayG] ⚠️  payg_secure_hash is EMPTY — hash will be incorrect!")

    message = f"{mid}|{unique_request_id}|{order_amount}"
    sig = hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest().upper()

    print(f"[PayG] compute_hash: mid={mid} | rid={unique_request_id} | amount={order_amount} | hash={sig[:8]}...")
    return sig


def verify_callback_hash(response_data: dict) -> bool:
    received_hash = (response_data.get("HashData") or "").strip().upper()
    if not received_hash:
        print("[PayG] verify_callback_hash: no HashData in payload — returning False")
        return False

    mid           = response_data.get("MID", "")
    unique_req_id = response_data.get("UniqueRequestId", "")
    order_amount  = str(response_data.get("OrderAmount", ""))
    expected      = compute_hash(mid, unique_req_id, order_amount)

    result = hmac.compare_digest(expected, received_hash)
    print(f"[PayG] verify_callback_hash: match={result}")
    return result


# ── Order creation ─────────────────────────────────────────────────────────

def _build_payg_payload(
    *,
    order_id: int,
    order_number: str,
    amount: float,
    customer_name: str,
    customer_email: str,
    customer_phone: Any,
    billing_address: str,
    billing_city: str,
    billing_state: str,
    billing_zip: str,
    redirect_url: str,
    shipping_state: Optional[str] = None,
    accepted_payment_types: str = "",
    payment_type: str = "",
) -> tuple[dict, str]:
    """Build the common PayG order payload. Returns (payload, unique_request_id)."""
    mid               = _cfg("payg_mid")

    unique_request_id = f"ORD{order_id}-{uuid.uuid4().hex[:8].upper()}"
    amount_str        = f"{float(amount):.2f}"

    if float(amount_str) <= 0:
        raise ValueError(
            f"Cannot create PayG order: amount={amount_str} must be > 0 (order_id={order_id})."
        )

    now_str     = datetime.now().strftime("%m%d%Y")
    hash_data   = compute_hash(mid, unique_request_id, amount_str)
    clean_phone = _sanitize_phone(customer_phone)

    name_parts = (customer_name or "Customer").strip().split(" ", 1)
    first_name = name_parts[0]
    last_name  = name_parts[1] if len(name_parts) > 1 else ""

    product_data_str = json.dumps({"PaymentReason": f"Order {order_number}"})

    sep = "&" if "?" in redirect_url else "?"
    redirect_url_with_ids = f"{redirect_url}{sep}oid={order_id}&rid={unique_request_id}"

    effective_shipping_state = (shipping_state or billing_state or "").strip()
    effective_billing_state  = (billing_state or "").strip()

    payload = {
        "MID":             mid,
        "UniqueRequestId": unique_request_id,
        # MerchantKeyId intentionally omitted:
        # PayG provided "M" (non-numeric). Their API declares it Int64,
        # so any string value causes 400. PayG identifies the merchant
        # from the Authorization header, so this field can be omitted.
        # Restore once PayG provides a real numeric MerchantKeyId.
        "OrderAmount":     f"{amount:.2f}",
        "UserDefinedData": {
            "UserDefined1": str(order_id),
            "UserDefined2": str(order_number),
        },
        "ProductData":        product_data_str,
        "RequestDateTime":    now_str,
        "RedirectUrl":        redirect_url_with_ids,
        "TransactionData": {
            "AcceptedPaymentTypes":      accepted_payment_types,
            "PaymentType":               payment_type,
            "SurchargeType":             "",
            "SurchargeValue":            "",
            "RefTransactionId":          "",
            "IndustrySpecificationCode": "",
            "PartialPaymentOption":      "",
        },
        "OrderAmountData": {
            "AmountTypeDesc": "1",
            "Amount":         amount_str,
        },
        "OrderType": "",
        "CustomerData": {
            "CustomerId":        str(order_id),
            "CustomerNotes":     f"Order {order_number}",
            "FirstName":         first_name,
            "LastName":          last_name,
            "MobileNo":          clean_phone,
            "Email":             customer_email or "",
            "EmailReceipt":      "true",
            "BillingAddress":    billing_address or "",
            "BillingCity":       billing_city or "",
            "BillingState":      effective_billing_state,
            "BillingCountry":    "India",
            "BillingZipCode":    billing_zip or "",
            "ShippingFirstName": first_name,
            "ShippingLastName":  last_name,
            "ShippingAddress":   billing_address or "",
            "ShippingCity":      billing_city or "",
            "ShippingState":     effective_shipping_state,
            "ShippingCountry":   "India",
            "ShippingZipCode":   billing_zip or "",
            "ShippingMobileNo":  clean_phone,
        },
        "IntegrationData": {
            "UserName":        first_name,
            "Source":          "3213",
            "IntegrationType": "",
            "HashData":        hash_data,
            "PlatformId":      "1",
        },
    }
    return payload, unique_request_id


def create_payg_order(
    *,
    order_id: int,
    order_number: str,
    amount: float,
    customer_name: str,
    customer_email: str,
    customer_phone: Any,
    billing_address: str,
    billing_city: str,
    billing_state: str,
    billing_zip: str,
    redirect_url: str,
    shipping_state: Optional[str] = None,
    accepted_payment_types: str = "",
) -> dict:
    """
    Standard order creation — used for Card / Debit / Credit / Net Banking.
    Returns PaymentProcessUrl for the frontend to open in WebView/browser.

    accepted_payment_types:
      ""      → default (website/desktop, unchanged) — PayG shows combined
                QR + Debit + Credit screen.
      "DB|CC" → mobile "Card" button — PayG shows only Debit/Credit, no QR.
    """
    """
    Standard order creation — used for Card / Debit / Credit / Net Banking.
    Returns PaymentProcessUrl for the frontend to open in WebView/browser.
    """
    payload, unique_request_id = _build_payg_payload(
        order_id=order_id, order_number=order_number, amount=amount,
        customer_name=customer_name, customer_email=customer_email,
        customer_phone=customer_phone, billing_address=billing_address,
        billing_city=billing_city, billing_state=billing_state,
        billing_zip=billing_zip, redirect_url=redirect_url,
        shipping_state=shipping_state,
        accepted_payment_types=accepted_payment_types,
        payment_type="",
    )

    # Create API uses the full 4-part auth header
    headers = {
        "Authorization": _build_auth_header(),
        "Content-Type":  "application/json",
    }

    create_url = _get_create_url()
    print(f"[PayG] Outbound IP: {httpx.get('https://api.ipify.org').text}")
    print(f"[PayG] Calling headers------{headers}")
    print(f"[PayG] Calling ---{create_url}")
    print(f"[PayG] MID={payload['MID']} | Amount={payload['OrderAmount']} | Phone={payload['CustomerData']['MobileNo']}")
    print(f"[PayG] RedirectUrl={payload['RedirectUrl']}")

    try:
        resp = _safe_json_post(create_url, payload, headers, timeout=15.0)
        print(f"[PayG] HTTP {resp.status_code} from {create_url}")
        print(f"[PayG] Response body: {resp.text[:1000]}")
        resp.raise_for_status()
        data = resp.json()

    except httpx.ConnectTimeout as exc:
        raise RuntimeError(
            "PayG gateway unreachable (connect timeout). "
            "Possible causes: network blocking uatapiv2.payg.in, or PayG UAT server down."
        ) from exc

    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"PayG HTTP error {exc.response.status_code}: {exc.response.text}"
        ) from exc

    except RuntimeError:
        raise

    except Exception as exc:
        raise RuntimeError(f"PayG request failed: {exc}") from exc

    processing_url = data.get("PaymentProcessUrl") or data.get("ProcessingUrl")
    if not processing_url:
        error_msg = (
            data.get("ResponseMessage") or data.get("responseMessage")
            or data.get("Message") or data.get("message")
            or data.get("Error") or data.get("error")
            or "No PaymentProcessUrl in response"
        )
        raise RuntimeError(f"PayG order creation failed — {error_msg}. Full response: {json.dumps(data)}")

    data["_unique_request_id"] = unique_request_id
    return data


# ── UPI Intent order creation (mobile) ─────────────────────────────────────

def create_payg_upi_intent_order(
    *,
    order_id: int,
    order_number: str,
    amount: float,
    customer_name: str,
    customer_email: str,
    customer_phone: Any,
    billing_address: str,
    billing_city: str,
    billing_state: str,
    billing_zip: str,
    redirect_url: str,
    shipping_state: Optional[str] = None,
) -> dict:
    """
    Calls PayG's /payment/api/order/CreateIntent endpoint (PaymentType=UPIINTENT).
    Returns a response containing 'UpiLink' (upi://pay?...) — the mobile
    frontend opens this URI directly to launch the user's UPI app
    (GPay/PhonePe/Paytm/etc).

    Per PayG support: same auth header / headers as the standard Create API,
    just a different URL and PaymentType="UPIINTENT".
    """
    payload, unique_request_id = _build_payg_payload(
        order_id=order_id, order_number=order_number, amount=amount,
        customer_name=customer_name, customer_email=customer_email,
        customer_phone=customer_phone, billing_address=billing_address,
        billing_city=billing_city, billing_state=billing_state,
        billing_zip=billing_zip, redirect_url=redirect_url,
        shipping_state=shipping_state,
        accepted_payment_types="",
        payment_type="UPIINTENT",
    )

    headers = {
        "Authorization": _build_upi_intent_auth_header(),
        "Content-Type":  "application/json",
    }

    intent_url = _get_intent_url()
    print(f"[PayG] Calling UPI Intent ---{intent_url}")
    print(f"[PayG] MID={payload['MID']} | Amount={payload['OrderAmount']}")

    try:
        resp = _safe_json_post(intent_url, payload, headers, timeout=15.0)
        print(f"[PayG] HTTP {resp.status_code} from {intent_url}")
        print(f"[PayG] Response body: {resp.text[:1000]}")
        resp.raise_for_status()
        data = resp.json()

    except httpx.ConnectTimeout as exc:
        raise RuntimeError(
            "PayG UPI Intent gateway unreachable (connect timeout)."
        ) from exc

    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"PayG UPI Intent HTTP error {exc.response.status_code}: {exc.response.text}"
        ) from exc

    except Exception as exc:
        raise RuntimeError(f"PayG UPI Intent request failed: {exc}") from exc

    upi_link = data.get("UpiLink")
    if not upi_link:
        error_msg = (
            data.get("PaymentResponseText") or data.get("ResponseMessage")
            or data.get("Message") or data.get("Error")
            or "No UpiLink in response"
        )
        raise RuntimeError(f"PayG UPI Intent failed — {error_msg}. Full response: {json.dumps(data)}")

    data["_unique_request_id"] = unique_request_id
    return data




def get_payg_order_detail(
    order_key_id: Optional[str] = None,
    unique_request_id: Optional[str] = None,
    mid: Optional[str] = None,
    bearer_token: Optional[str] = None,
) -> dict:
    """
    Fetch live payment status from PayG /payment/api/order/detail.

    IMPORTANT — Auth format:
      This endpoint uses Basic base64(auth_key:auth_token) ONLY.
      Do NOT use the Create API auth format (4 parts) here — PayG
      returns HTTP 400 code 9020 'Invalid Authorization' if you do.

    PayG official PaymentResponseCode values:
      1  → Approved   ✓  (return immediately)
      2  → Declined   ✗  (return immediately)
      4  → Pending       (retry)
      null/absent        (retry)

    NOTE — IP Allowlist:
      This API is IP-allowlisted by PayG. If your server IP is not
      registered in the PayG merchant portal, every call returns 403.
      Fix: PayG portal → Settings → IP Whitelist → Add server IP.
      Run GET /api/Payment/OutboundIp to find the server's public IP.
    """
    _mid = (mid or _cfg("payg_mid")).strip()

    if not order_key_id and not unique_request_id:
        raise ValueError("Provide either order_key_id or unique_request_id")

    body: dict = {"MID": _mid}
    if order_key_id:
        body["OrderKeyId"] = order_key_id
    else:
        body["UniqueRequestId"] = unique_request_id

    ref = order_key_id or unique_request_id

    # PayG confirmed: Detail API uses the same key:token:M:mid format as
    # CreateIntent (literal "M", not the merchantKeyId used by Create).
    # Previously no Authorization header was sent here, which produced
    # HTTP 500 on every call.
    headers = {
        "Authorization": _build_detail_auth_header(),
        "Content-Type":  "application/json",
    }

    detail_url   = _get_detail_url()
    MAX_ATTEMPTS = 3
    RETRY_DELAY  = 2.0

    last_data: dict = {}
    last_exc: Optional[Exception] = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            print(f"[PayG] Detail API attempt {attempt}/{MAX_ATTEMPTS} | ref={ref!r} | url={detail_url}")

            resp = _safe_json_post(detail_url, body, headers, timeout=15.0)

            print(f"[PayG] Detail API HTTP {resp.status_code} | body={resp.text[:500]!r}")

            # 403 = IP not whitelisted — deterministic, no point retrying
            if resp.status_code == 403:
                msg = resp.text.strip()
                print(
                    f"[PayG] Detail API ❌ 403 FORBIDDEN — '{msg}'\n"
                    f"  Your server IP is NOT whitelisted in the PayG portal.\n"
                    f"  1. Call GET /api/Payment/OutboundIp to find your server IP\n"
                    f"  2. PayG portal → Settings → IP Whitelist → Add that IP"
                )
                raise RuntimeError(
                    f"PayG Detail API blocked (403): {msg}. "
                    f"Add this server's IP to the PayG merchant portal IP whitelist."
                )

            # 400 with 9020 = wrong auth format — no point retrying
            if resp.status_code == 400:
                body_text = resp.text or ""
                if "9020" in body_text or "Invalid Authorization" in body_text:
                    print(
                        f"[PayG] Detail API ❌ 400 Invalid Authorization (code 9020)\n"
                        f"  Check payg_auth_key and payg_auth_token in AppSettings.\n"
                        f"  These must exactly match the credentials in the PayG portal."
                    )
                    raise RuntimeError(
                        f"PayG Detail API 400 Invalid Authorization: "
                        f"payg_auth_key or payg_auth_token is incorrect. "
                        f"Verify credentials in PayG merchant portal."
                    )

            resp.raise_for_status()
            data = resp.json()

            raw_code = data.get("PaymentResponseCode")
            code     = _parse_response_code(raw_code)

            # Inject normalised code so callers always get an int
            data["PaymentResponseCode"] = code

            print(
                f"[PayG] Detail API attempt {attempt} → "
                f"raw_code={raw_code!r} → normalised={code} | "
                f"OrderStatus={data.get('OrderStatus')!r}"
            )

            # Resolved codes — exit immediately
            if code in (1, 2):
                return data

            # code=4 (Pending) or code=-1 (null/unknown) — retry
            last_data = data

        except httpx.HTTPStatusError as exc:
            last_exc = RuntimeError(
                f"PayG Detail HTTP error {exc.response.status_code}: {exc.response.text}"
            )
            print(f"[PayG] Detail API attempt {attempt} ❌ HTTP {exc.response.status_code}")

        except RuntimeError:
            raise  # our own errors (403/400 guards) — don't swallow

        except Exception as exc:
            last_exc = RuntimeError(f"PayG Detail request failed: {exc}")
            print(f"[PayG] Detail API attempt {attempt} ❌ {exc}")

        if attempt < MAX_ATTEMPTS:
            print(f"[PayG] Detail API — waiting {RETRY_DELAY}s before retry...")
            time.sleep(RETRY_DELAY)

    # All attempts exhausted
    if last_data:
        print(
            f"[PayG] Detail API: all {MAX_ATTEMPTS} attempts done — "
            f"returning last data (code={last_data.get('PaymentResponseCode')!r})"
        )
        return last_data

    if last_exc:
        raise last_exc

    raise RuntimeError(
        f"PayG Detail API: no response after {MAX_ATTEMPTS} attempts for ref={ref!r}"
    )


# ── Response code → order state ────────────────────────────────────────────

def payment_code_to_order_state(code: int) -> str:
    """
    Map PayG PaymentResponseCode to internal order state string.

    Only maps PayG's 3 official codes (1, 2, 4) plus our internal
    sentinel -1 (null / IP-blocked / unknown).
    """
    return {
         1: "Confirmed",        # Approved ✓
         2: "Payment Failed",   # Declined / Failed ✗
         4: "Payment Pending",  # Pending — awaiting bank confirmation
        -1: "AwaitingPayment",  # Unknown — null or auth error; keep order alive
    }.get(code, "AwaitingPayment")