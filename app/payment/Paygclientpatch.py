# app/payment/payg_client.py  — PATCH: bearer_token support
#
# Add `bearer_token: Optional[str] = None` to get_payg_order_detail().
# When provided it is forwarded as Authorization: Bearer <token> in the
# outbound HTTP request to PayG's Detail API.
#
# Replace ONLY the get_payg_order_detail function in your existing payg_client.py.

from __future__ import annotations
from typing import Optional
import httpx
from app.shared.app_settings import get_setting


def get_payg_order_detail(
    *,
    unique_request_id: Optional[str] = None,
    order_key_id: Optional[str] = None,
    bearer_token: Optional[str] = None,          # ← NEW: forwarded to PayG Detail API
) -> dict:
    """
    Call PayG's Detail API.

    bearer_token:
      Pass the current user's JWT (extracted from the incoming request) or a
      backend service token.  PayG UAT Detail API is open but still needs any
      valid Bearer token in the Authorization header — this parameter supplies it.
      If None, the request is sent without an Authorization header (falls back to
      previous behaviour, which works when PayG does not enforce auth on Detail).
    """
    # mid       = get_setting("payg_mid", "")
    auth_key  = get_setting("payg_auth_key", "")
    auth_token = get_setting("payg_auth_token", "")
    base_url  = get_setting("payg_base_url", " https://uatapiv2.payg.in/payment/api/order/detail")

    if not unique_request_id and not order_key_id:
        raise ValueError("get_payg_order_detail: supply unique_request_id OR order_key_id")

    payload = {
        # "MerchantId": mid,
        "AuthKey":    auth_key,
        "AuthToken":  auth_token,
    }
    if unique_request_id:
        payload["UniqueRequestId"] = unique_request_id
    if order_key_id:
        payload["OrderKeyId"] = order_key_id

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept":       "application/json",
    }

    # ── Forward auth token to PayG Detail API when provided ──────────────
    # PayG UAT Detail API requires any valid Bearer in Authorization header.
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
        print("Auth header being sent:", headers["Authorization"])
        print("URL being called:", base_url)
        # print("Body being sent:", body)

    with httpx.Client(timeout=15) as client:
        resp = client.post(
            f"{base_url}/api/Order/Detail",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()