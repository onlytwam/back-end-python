"""
app/ekart/client.py  — Ekart Elite Logistics API Client
========================================================
Covers every endpoint from the Ekart Elite API spec:

  Auth:              POST /integrations/v2/auth/token/{client_id}
  Create shipment:   PUT  /api/v1/package/create         (forward & reverse)
  Cancel shipment:   DELETE /api/v1/package/cancel?tracking_id=...
  Track (v1):        GET  /api/v1/track/{id}             (open, no auth)
  Track (elite):     GET  /data/v1/elite/track/{wbn}
  Label:             POST /api/v1/package/label
  Manifest:          POST /data/v2/generate/manifest
  NDR action:        POST /api/v2/package/ndr
  Address V2:        POST /api/v2/address  (register pickup/return warehouse)
                     GET  /api/v2/addresses (list registered addresses)
  Serviceability v2: GET  /api/v2/serviceability/{pincode}
  Serviceability v3: POST /data/v3/serviceability
  Shipping estimate: POST /data/pricing/estimate
  Dispatch date:     POST /data/shipment/dispatch-date
  Bulk shipments:    multiple PUT /api/v1/package/create calls
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Optional

try:
    import httpx
except ModuleNotFoundError:
    httpx = None  # type: ignore

from .schemas import (
    EkartConfig,
    CreateShipmentRequest,
    CreateReturnShipmentRequest,
    EkartShipmentResponse,
    EkartTrackingResponse,
    EkartTrackingEvent,
    CancelShipmentRequest,
    CancelShipmentResponse,
    EKART_TO_ORDER_STATUS,
)

EKART_BASE_URL = "https://app.elite.ekartlogistics.in"

# ── Token cache (in-process, per client_id) ──────────────────────────────────
_token_cache: dict[str, dict] = {}
_TOKEN_BUFFER_SECS = 300  # refresh 5 min before expiry


def _cache_token(client_id: str, token: str, expires_in: int = 86400):
    _token_cache[client_id] = {
        "token": token,
        "expires_at": time.time() + expires_in - _TOKEN_BUFFER_SECS,
    }


def _get_cached_token(client_id: str) -> Optional[str]:
    entry = _token_cache.get(client_id)
    if entry and time.time() < entry["expires_at"]:
        return entry["token"]
    return None


# ── DB config loader ──────────────────────────────────────────────────────────

def load_ekart_config_from_db(db) -> Optional["EkartDbConfig"]:
    try:
        from app.shipment.config_model import ShipmentConfigurations
        from sqlalchemy import or_, func as sa_func
        # FIX Bug 3: stateId column is nullable and case-sensitive in PostgreSQL.
        # Original filter `stateId == "Active"` fails when:
        #   - stateId is NULL  (not yet set in DB row)
        #   - stateId is "active" (lowercase) or "ACTIVE"
        # This caused the entire Ekart integration to fall back to MOCK MODE,
        # returning fake AWB numbers instead of real ones.
        row = (
            db.query(ShipmentConfigurations)
            .filter(
                ShipmentConfigurations.deliveryAgent.ilike("EKart%"),
                ShipmentConfigurations.deletedInd == False,
                or_(
                    ShipmentConfigurations.stateId == None,  # noqa: E711
                    sa_func.lower(ShipmentConfigurations.stateId) == "active",
                ),
            )
            .first()
        )
        if not row:
            print("[EkartClient] No active EKart config found in DB — using mock mode")
            return None
        return EkartDbConfig(
            client_id=row.clientId or "",
            access_token_url=row.accessTokenURL or "",
            create_shipment_url=row.createShipmentURL or "",
            cancel_shipment_url=row.cancelShipmentURL or "",
            track_shipment_url=row.trackShipmentURL or "",
            waybill_url=row.wayBillURL or "",
            username=row.userName or "",
            password=row.password or "",
            return_name=row.returnName or "",
            return_phone=row.returnPhone or "",
            return_address_line1=row.returnAddressLine1 or "",
            return_address_line2=row.returnAddressLine2 or "",
            return_city=row.returnCity or "",
            return_state=row.returnState or "",
            return_pincode=row.returnPinCode or "",
            return_country=row.returnCountry or "India",
            pickup_alias=row.pickupAlias or "",  # alias registered via POST /api/v2/address
        )
    except Exception as e:
        print(f"[EkartClient] DB config load failed: {e}")
        return None


class EkartDbConfig:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    @property
    def is_complete(self) -> bool:
        return bool(
            self.access_token_url
            and self.create_shipment_url
            and self.username
            and self.password
        )


# ── Ekart Elite Client ────────────────────────────────────────────────────────

class EkartClient:
    def __init__(self, db_config: Optional[EkartDbConfig] = None):
        self.db_config = db_config
        self.mock_mode = (
            httpx is None
            or db_config is None
            or not db_config.is_complete
        )
        if self.mock_mode:
            reason = "httpx not installed" if httpx is None else (
                "no DB config" if db_config is None else "incomplete config"
            )
            print(f"[EkartClient] Running in MOCK mode ({reason})")

    # ── Authentication ────────────────────────────────────────────────────────
    # POST /integrations/v2/auth/token/{client_id}
    # Body: {"username": "...", "password": "..."}
    # Response: {"access_token": "...", "expires_in": N, "token_type": "Bearer"}

    async def _get_token(self) -> str:
        if self.mock_mode:
            return "mock-token"

        cfg = self.db_config
        cached = _get_cached_token(cfg.client_id)
        if cached:
            return cached

        print(f"[EkartClient] Fetching new token from {cfg.access_token_url!r}...")
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    cfg.access_token_url,
                    json={"username": cfg.username, "password": cfg.password},
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            import traceback
            print(f"[EkartClient] _get_token RAW exception: {type(e).__name__}: {e!r}")
            traceback.print_exc()
            raise

        token = data.get("access_token") or data.get("token") or ""
        expires_in = int(data.get("expires_in", 86400))
        _cache_token(cfg.client_id, token, expires_in)
        print(f"[EkartClient] Got new token, expires in {expires_in}s")
        return token

    def _auth_headers(self, token: str) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }

    # ── Create Forward/Return Shipment ────────────────────────────────────────
    # PUT /api/v1/package/create
    # Response: {"status": true, "tracking_id": "...", "vendor": "...", "barcodes": {...}}

    async def create_shipment(self, request: CreateShipmentRequest) -> EkartShipmentResponse:
        if self.mock_mode:
            return self._mock_create_shipment(request)

        cfg = self.db_config
        try:
            token = await self._get_token()
            payload = self._build_create_payload(request)

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.put(
                    cfg.create_shipment_url,
                    json=payload,
                    headers=self._auth_headers(token),
                )
                resp.raise_for_status()
                data = resp.json()

            # FIX 3: Ekart may return {"status": true} (bool) OR {"status": "true"} (string).
            # Using "is True" would evaluate "true" == False and wrongly report failure.
            raw_status = data.get("status")
            success = raw_status is True or str(raw_status).lower() == "true"
            awb = (
                data.get("tracking_id")
                or data.get("awb_number")
                or data.get("awb")
                or data.get("waybillNumber")
                or data.get("wbn")
            )

            return EkartShipmentResponse(
                success=success,
                awb_number=str(awb) if awb else None,
                tracking_url=f"{EKART_BASE_URL}/track/{awb}" if awb else None,
                courier_name=data.get("vendor") or "Ekart",
                estimated_delivery=None,
                # raw_response removed — not in EkartShipmentResponse schema
            )

        except Exception as e:
            import traceback
            print(f"[EkartClient] create_shipment RAW exception: {type(e).__name__}: {e!r}")
            traceback.print_exc()
            err = self._extract_error(e)
            print(f"[EkartClient] create_shipment error: {err}")
            return EkartShipmentResponse(success=False, error_message=err)

    def _build_create_payload(self, req: CreateShipmentRequest) -> dict:
        """
        Build Ekart Elite v1 create-shipment payload.
        phone and pin MUST be integers. weight is in GRAMS.
        payment_mode: "COD" | "Prepaid" (case-sensitive)
        """
        cfg = self.db_config

        def _phone_int(phone: str) -> int:
            # Used for locationV1.phone — spec type: integer (min 1000000000)
            digits = "".join(c for c in (phone or "") if c.isdigit())
            return int(digits[-10:]) if len(digits) >= 10 else int(digits or "0")

        def _phone_str(phone: str) -> str:
            # Used for consignee_alternate_phone — spec type: string, minLength: 10
            digits = "".join(c for c in (phone or "") if c.isdigit())
            return digits[-10:] if len(digits) >= 10 else digits.zfill(10)

        def _pin_int(pin: str) -> int:
            digits = "".join(c for c in (pin or "") if c.isdigit())
            return int(digits[:6]) if digits else 0

        ekart_payment_mode = "COD" if req.payment_mode == "COD" else "Prepaid"

        # ── Pickup / Return location ──────────────────────────────────────────
        # If a pickup alias is registered with Ekart (via POST /api/v2/address),
        # send just {"name": alias} — Ekart autofills the rest from their records.
        # This avoids SWIFT_RESOURCE_NOT_FOUND_EXCEPTION which occurs when sending
        # a full address object that Ekart has not seen during onboarding.
        # The alias is stored in ShipmentConfigurations.PickupAlias after registration.
        if cfg.pickup_alias:
            pickup_location = {"name": cfg.pickup_alias}
        else:
            pickup_location = {
                "name":    cfg.return_name or "Warehouse",
                "phone":   _phone_int(cfg.return_phone),
                "address": cfg.return_address_line1 or "",
                "city":    cfg.return_city or "",
                "state":   cfg.return_state or "",
                "country": cfg.return_country or "India",
                "pin":     _pin_int(cfg.return_pincode),
            }

        addr = req.consignee_address
        drop_location = {
            "name":    addr.name or req.consignee_name,
            "phone":   _phone_int(addr.phone or req.consignee_phone),
            "address": addr.address_line1 + (f", {addr.address_line2}" if addr.address_line2 else ""),
            "city":    addr.city or "",
            "state":   addr.state or "",
            "country": addr.country or "India",
            "pin":     _pin_int(addr.pincode),
        }

        # spec: weight is integer in grams (minimum: 1)
        # CreateShipmentRequest.weight field is already in grams (default=500g)
        weight_grams = max(1, int(req.weight or 500))

        # Tax note: price is MRP-inclusive (tax already embedded in price).
        # No separate tax is charged at checkout. Per Ekart spec:
        #   tax_value = 0  (minimum: 0 — valid)
        #   taxable_amount = total_amount  (since total = taxable + 0)
        #   commodity_value = str(taxable_amount)
        #   consignee_gst_amount = 0
        total_amt    = float(req.total_amount or 0)
        tax_val      = 0.0
        taxable_amt  = max(1.0, total_amt)   # spec minimum: 1

        payload = {
            "order_number":              req.order_number or str(req.order_id),
            "invoice_number":            req.invoice_number or req.order_number or str(req.order_id),
            "invoice_date":              req.invoice_date.strftime("%d-%m-%Y") if req.invoice_date else datetime.now().strftime("%d-%m-%Y"),
            "consignee_name":            req.consignee_name or addr.name,
            # FIX: Use warehouse/return phone as alternate — NOT the customer's own phone.
            # Ekart's SWIFT validation rejects if drop_location.phone == consignee_alternate_phone.
            # drop_location.phone is already set to the customer phone above.
            "consignee_alternate_phone": _phone_str(cfg.return_phone or ""),
            "payment_mode":              ekart_payment_mode,
            "cod_amount":                total_amt if ekart_payment_mode == "COD" else 0,
            "total_amount":              total_amt,
            "tax_value":                 tax_val,
            "taxable_amount":            taxable_amt,
            "commodity_value":           str(int(taxable_amt)),
            "consignee_gst_amount":      tax_val,
            "seller_name":               cfg.return_name or "Seller",
            "seller_address":            cfg.return_address_line1 or "",
            "seller_gst_tin":            "",
            "products_desc":             req.product_description or "Fashion Apparel",
            "category_of_goods":         "Fashion",
            "hsn_code":                  ",".join(i.hsn_code for i in req.items if i.hsn_code) if req.items else "",
            "quantity":                  sum(i.quantity for i in req.items) if req.items else 1,
            "weight":                    weight_grams,
            "length":                    int(req.length or 30),
            "height":                    int(req.height or 5),
            "width":                     int(req.width or 25),
            "drop_location":             drop_location,
            "pickup_location":           pickup_location,
            "return_location":           pickup_location,
            "return_reason":             "",
        }

        # ── preferred_dispatch_date ───────────────────────────────────────────
        # Express and Same Day → request immediate pickup (today's date).
        # Normal and Free     → omit field (Ekart picks up on their schedule).
        # Ekart has NO serviceType on /api/v1/package/create — delivery speed is
        # determined by your account contract, not this field.
        dispatch_date = req.preferred_dispatch_date  # explicit override wins
        if not dispatch_date and req.shipping_type_name:
            name_lower = req.shipping_type_name.strip().lower()
            if name_lower in ("express", "same day", "sameday", "same-day"):
                dispatch_date = datetime.now().strftime("%Y-%m-%d")
        if dispatch_date:
            payload["preferred_dispatch_date"] = dispatch_date
            print(f"[EkartClient] shipping_type={req.shipping_type_name!r} → preferred_dispatch_date={dispatch_date}")

        return payload

    # ── Bulk Shipment Creation ────────────────────────────────────────────────
    # Sends multiple concurrent PUT /api/v1/package/create requests.
    # Returns list of (order_id, EkartShipmentResponse).

    async def create_bulk_shipments(
        self, requests: list[CreateShipmentRequest]
    ) -> list[dict]:
        """
        Create multiple forward shipments concurrently (max 10 at once).
        Returns: [{"order_id": ..., "success": ..., "awb_number": ..., "error": ...}]
        """
        async def _one(req: CreateShipmentRequest) -> dict:
            result = await self.create_shipment(req)
            return {
                "order_id":   req.order_id,
                "order_number": req.order_number,
                "success":    result.success,
                "awb_number": result.awb_number,
                "tracking_url": result.tracking_url,
                "error":      result.error_message,
            }

        # Process in batches of 10 to avoid overwhelming the API
        results = []
        batch_size = 10
        for i in range(0, len(requests), batch_size):
            batch = requests[i : i + batch_size]
            batch_results = await asyncio.gather(*[_one(r) for r in batch])
            results.extend(batch_results)

        return results

    # ── Create Return Shipment ────────────────────────────────────────────────
    # payment_mode = "Pickup" for reverse shipments

    async def create_return_shipment(self, request: CreateReturnShipmentRequest) -> EkartShipmentResponse:
        if self.mock_mode:
            return self._mock_create_return_shipment(request)

        cfg = self.db_config

        def _phone_int(phone: str) -> int:
            digits = "".join(c for c in (phone or "") if c.isdigit())
            return int(digits[-10:]) if len(digits) >= 10 else int(digits or "0")

        def _pin_int(pin: str) -> int:
            digits = "".join(c for c in (pin or "") if c.isdigit())
            return int(digits[:6]) if digits else 0

        try:
            token = await self._get_token()
            pickup = request.pickup_address
            ret_addr = request.return_address

            payload = {
                "order_number":              str(request.order_id),
                "invoice_number":            str(request.order_id),
                "invoice_date":              datetime.now().strftime("%d-%m-%Y"),
                "consignee_name":            request.pickup_name,
                "consignee_alternate_phone": _phone_int(request.pickup_phone),
                "payment_mode":              "Pickup",
                "return_reason":             request.return_reason or "Customer return",
                "total_amount":              0,
                "tax_value":                 0,
                "taxable_amount":            0,
                "commodity_value":           "0",
                "consignee_gst_amount":      0,
                "cod_amount":                0,
                "seller_name":               ret_addr.name,
                "seller_address":            ret_addr.address_line1,
                "seller_gst_tin":            "",
                "products_desc":             "Return — Fashion Apparel",
                "category_of_goods":         "Fashion",
                "quantity":                  sum(i.quantity for i in request.items) if request.items else 1,
                "weight":                    500,
                "length":                    30,
                "height":                    5,
                "width":                     25,
                "drop_location": {
                    "name":    request.pickup_name,
                    "phone":   _phone_int(request.pickup_phone),
                    "address": pickup.address_line1,
                    "city":    pickup.city,
                    "state":   pickup.state,
                    "country": pickup.country or "India",
                    "pin":     _pin_int(pickup.pincode),
                },
                "pickup_location": {
                    "name":    ret_addr.name,
                    "phone":   _phone_int(ret_addr.phone),
                    "address": ret_addr.address_line1,
                    "city":    ret_addr.city,
                    "state":   ret_addr.state,
                    "country": ret_addr.country or "India",
                    "pin":     _pin_int(ret_addr.pincode),
                },
            }

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.put(
                    cfg.create_shipment_url,
                    json=payload,
                    headers=self._auth_headers(token),
                )
                resp.raise_for_status()
                data = resp.json()

            awb = data.get("tracking_id") or data.get("awb")
            return EkartShipmentResponse(
                success=data.get("status") is True,
                awb_number=str(awb) if awb else None,
                tracking_url=f"{EKART_BASE_URL}/track/{awb}" if awb else None,
                courier_name="Ekart",
            )

        except Exception as e:
            err = self._extract_error(e)
            print(f"[EkartClient] create_return_shipment error: {err}")
            return EkartShipmentResponse(success=False, error_message=err)

    # ── Track Shipment (v1 — open API, no auth) ───────────────────────────────
    # GET /api/v1/track/{id}

    async def track_shipment(self, awb_number: str) -> EkartTrackingResponse:
        if self.mock_mode:
            return self._mock_track_shipment(awb_number)

        try:
            track_base = (
                self.db_config.track_shipment_url
                if self.db_config
                else f"{EKART_BASE_URL}/api/v1/track"
            )
            url = track_base.rstrip("/") + f"/{awb_number}"

            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(url, headers={"Content-Type": "application/json"})
                resp.raise_for_status()
                data = resp.json()

            events = []
            track = data.get("track") or {}
            raw_history = data.get("history") or track.get("history") or []

            if awb_number in data:
                raw_data = data[awb_number]
                raw_history = raw_data.get("history") or []
                current_status = raw_data.get("status") or "Unknown"
            else:
                current_status = track.get("status") or data.get("status") or "In Transit"

            for evt in raw_history:
                ts_str = evt.get("event_date_iso8601") or evt.get("event_date") or evt.get("timestamp")
                try:
                    ts = datetime.fromisoformat(ts_str.replace("+0530", "+05:30")) if ts_str else datetime.now(timezone.utc)
                except Exception:
                    ts = datetime.now(timezone.utc)

                events.append(EkartTrackingEvent(
                    status=evt.get("public_description") or evt.get("status") or "",
                    status_code=evt.get("status") or "UNK",
                    location=evt.get("city") or evt.get("hub_name") or "",
                    timestamp=ts,
                    remarks=evt.get("hub_notes") or evt.get("cs_notes") or "",
                ))

            return EkartTrackingResponse(
                awb_number=awb_number,
                current_status=current_status,
                current_status_code=current_status.upper().replace(" ", "_")[:10],
                expected_delivery=None,
                delivered_date=None,
                events=events,
            )

        except Exception as e:
            print(f"[EkartClient] track_shipment error: {e}")
            return EkartTrackingResponse(
                awb_number=awb_number,
                current_status="Tracking Unavailable",
                current_status_code="ERR",
                events=[],
            )

    # ── Track (Elite raw API) ─────────────────────────────────────────────────
    # GET /data/v1/elite/track/{wbn}  — returns full raw Ekart response

    async def track_shipment_elite(self, wbn: str) -> dict:
        if self.mock_mode:
            return {"mock": True, "wbn": wbn, "status": "In Transit"}

        try:
            token = await self._get_token()
            url = f"{EKART_BASE_URL}/data/v1/elite/track/{wbn}"
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(url, headers=self._auth_headers(token))
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            return {"error": str(e), "wbn": wbn}

    # ── Cancel Shipment ───────────────────────────────────────────────────────
    # DELETE /api/v1/package/cancel?tracking_id={id}

    async def cancel_shipment(self, request: CancelShipmentRequest) -> CancelShipmentResponse:
        if self.mock_mode:
            return CancelShipmentResponse(
                success=True, awb_number=request.awb_number,
                message="Cancelled (mock mode)",
            )

        cfg = self.db_config
        try:
            token = await self._get_token()

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.delete(
                    cfg.cancel_shipment_url,
                    params={"tracking_id": request.awb_number},
                    headers=self._auth_headers(token),
                )
                resp.raise_for_status()

            return CancelShipmentResponse(
                success=True,
                awb_number=request.awb_number,
                message="Shipment cancelled on Ekart",
            )

        except Exception as e:
            err = self._extract_error(e)
            print(f"[EkartClient] cancel_shipment error: {err}")
            return CancelShipmentResponse(
                success=False, awb_number=request.awb_number, error_message=err,
            )

    # ── Download Label ────────────────────────────────────────────────────────
    # POST /api/v1/package/label
    # Body: {"ids": ["awb1", "awb2", ...]}
    # Returns: PDF binary (or JSON if json_only=true)

    async def download_label(
        self, awb_numbers: list[str], json_only: bool = False
    ) -> bytes | dict:
        if self.mock_mode:
            return {"mock": True, "message": "Label would be returned as PDF bytes"}

        cfg = self.db_config
        try:
            token = await self._get_token()
            url = cfg.waybill_url or f"{EKART_BASE_URL}/api/v1/package/label"
            params = {"json_only": "true"} if json_only else {}

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    url,
                    json={"ids": awb_numbers},
                    headers=self._auth_headers(token),
                    params=params,
                )
                resp.raise_for_status()

            if json_only:
                return resp.json()
            return resp.content  # raw PDF bytes

        except Exception as e:
            err = self._extract_error(e)
            print(f"[EkartClient] download_label error: {err}")
            return {"error": err}

    # ── Download Manifest ─────────────────────────────────────────────────────
    # POST /data/v2/generate/manifest
    # Body: {"ids": ["awb1", "awb2", ...]}
    # Returns: JSON with manifest data

    async def download_manifest(self, awb_numbers: list[str]) -> dict:
        if self.mock_mode:
            return {
                "mock": True,
                "manifest_id": "MOCK-MANIFEST-001",
                "count": len(awb_numbers),
                "awb_numbers": awb_numbers,
            }

        try:
            token = await self._get_token()
            url = f"{EKART_BASE_URL}/data/v2/generate/manifest"

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    url,
                    json={"ids": awb_numbers},
                    headers=self._auth_headers(token),
                )
                resp.raise_for_status()
                return resp.json()

        except Exception as e:
            err = self._extract_error(e)
            print(f"[EkartClient] download_manifest error: {err}")
            return {"error": err}

    # ── NDR Action ────────────────────────────────────────────────────────────
    # POST /api/v2/package/ndr
    # NDR = Non-Delivery Report
    # ndrAction values: "RE_ATTEMPT", "RTO", "CONFIRM_DELIVERY"

    # ── NDR action enum mapping ──────────────────────────────────────────────
    # Spec ndr_action_enum_v2: ["Re-Attempt", "RTO"]
    # Old internal codes mapped here for backwards compatibility:
    _NDR_ACTION_MAP = {
        "RE_ATTEMPT":        "Re-Attempt",  # old internal code → spec value
        "Re-Attempt":        "Re-Attempt",  # already correct
        "RTO":               "RTO",
        "CONFIRM_DELIVERY":  None,           # NOT in spec — Ekart has no confirm endpoint
    }

    async def action_ndr(
        self,
        tracking_id: str,
        ndr_action: str,
        remarks: str = "",
        preferred_date: Optional[str] = None,  # "YYYY-MM-DD" for Re-Attempt
    ) -> dict:
        """
        Take action on a Non-Delivery Report shipment.

        ndr_action: "RE_ATTEMPT" | "Re-Attempt" | "RTO"
          ("CONFIRM_DELIVERY" is not in the Ekart spec and will return an error)

        preferred_date: "YYYY-MM-DD" string — converted to ms-epoch int64
          Required when action is "Re-Attempt" (must be within 7 days, not today)

        Spec: POST /api/v2/package/ndr
          Required fields: action (enum), wbn
          Optional fields: date (int64 ms), phone, address, instructions, links[]
        """
        # Map old internal codes to spec enum values
        spec_action = self._NDR_ACTION_MAP.get(ndr_action)
        if spec_action is None:
            return {
                "error": f"NDR action '{ndr_action}' is not supported by Ekart spec. "
                         f"Valid values: 'Re-Attempt', 'RTO'",
                "tracking_id": tracking_id,
            }

        if self.mock_mode:
            return {"mock": True, "wbn": tracking_id, "action": spec_action}

        try:
            token = await self._get_token()
            url = f"{EKART_BASE_URL}/api/v2/package/ndr"

            payload: dict = {
                "wbn":    tracking_id,   # spec field name: wbn (not tracking_id)
                "action": spec_action,   # spec field name: action (not ndrAction)
            }

            # optional: instructions (spec), formerly remarks
            if remarks:
                payload["instructions"] = remarks

            # Re-Attempt requires date as milliseconds int64, NOT a date string
            if spec_action == "Re-Attempt" and preferred_date:
                from datetime import datetime
                try:
                    dt = datetime.strptime(preferred_date, "%Y-%m-%d")
                    # spec: int64 ms epoch
                    payload["date"] = int(dt.timestamp() * 1000)
                except ValueError:
                    pass  # bad date format — omit rather than crash

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    url,
                    json=payload,
                    headers=self._auth_headers(token),
                )
                resp.raise_for_status()
                return resp.json()

        except Exception as e:
            err = self._extract_error(e)
            print(f"[EkartClient] action_ndr error: {err}")
            return {"error": err, "tracking_id": tracking_id}

    # ── Serviceability V2 ─────────────────────────────────────────────────────
    # GET /api/v2/serviceability/{pincode}
    # Returns: COD availability, forward/reverse serviceability

    async def check_serviceability_v2(self, pincode: str) -> dict:
        if self.mock_mode:
            return {
                "mock": True,
                "pincode": pincode,
                "cod": True,
                "forward_serviceable": True,
                "reverse_serviceable": True,
            }

        try:
            token = await self._get_token()
            url = f"{EKART_BASE_URL}/api/v2/serviceability/{pincode}"

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=self._auth_headers(token))
                resp.raise_for_status()
                return resp.json()

        except Exception as e:
            print(f"[EkartClient] serviceability_v2 error: {e}")
            return {"error": str(e), "pincode": pincode}

    # ── Serviceability V3 ─────────────────────────────────────────────────────
    # POST /data/v3/serviceability
    # Returns: list of available courier partners for pickup→drop pincodes

    async def check_serviceability_v3(
        self,
        pickup_pincode: str,
        drop_pincode: str,
        weight: float = 0.5,
        length: float = 30,
        width: float = 25,
        height: float = 5,
    ) -> list:
        if self.mock_mode:
            return [{
                "mock": True,
                "pickup_pincode": pickup_pincode,
                "drop_pincode": drop_pincode,
                "courier": "Ekart",
                "cod_available": True,
                "estimated_days": 3,
            }]

        try:
            token = await self._get_token()
            url = f"{EKART_BASE_URL}/data/v3/serviceability"
            # spec: all dimension fields are strings, weight in grams as string
            # caller passes weight in kg (default 0.5), so multiply by 1000
            weight_grams_str = str(max(1, int(weight * 1000)))

            # spec serviceability_v3_request required fields:
            #   pickupPincode, dropPincode, length, width, height, weight (strings)
            #   paymentType (COD|Prepaid), invoiceAmount (string)
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    url,
                    json={
                        "pickupPincode": pickup_pincode,
                        "dropPincode":   drop_pincode,
                        "length":        str(int(length)),
                        "width":         str(int(width)),
                        "height":        str(int(height)),
                        "weight":        weight_grams_str,   # spec: string (not int)
                        "paymentType":   "Prepaid",          # spec REQUIRED: COD|Prepaid
                        "invoiceAmount": "1",                # spec REQUIRED: invoice value string
                    },
                    headers=self._auth_headers(token),
                )
                resp.raise_for_status()
                return resp.json()

        except Exception as e:
            print(f"[EkartClient] serviceability_v3 error: {e}")
            return [{"error": str(e)}]

    # ── Shipping Rate Estimate ────────────────────────────────────────────────
    # POST /data/pricing/estimate
    # Returns: estimated shipping rates / charges

    async def get_shipping_estimate(
        self,
        pickup_pincode: str,
        drop_pincode: str,
        weight: float = 0.5,
        payment_mode: str = "Prepaid",
        cod_amount: float = 0,
    ) -> dict:
        if self.mock_mode:
            # Sensible mock: flat ₹60 prepaid, ₹80 COD
            charge = 80.0 if payment_mode == "COD" else 60.0
            return {
                "mock": True,
                "pickup_pincode": pickup_pincode,
                "drop_pincode": drop_pincode,
                "estimated_charge": charge,
                "currency": "INR",
                "estimated_days": 3,
                "vendor": "Ekart",
            }

        try:
            token = await self._get_token()
            url = f"{EKART_BASE_URL}/data/pricing/estimate"
            # spec: weight in grams (integer); caller passes kg so multiply
            weight_grams = max(1, int(weight * 1000))

            # spec estimateRequest required fields (camelCase — NOT snake_case):
            #   billingClientType, shippingDirection, serviceType,
            #   pickupPincode, dropPincode, length, weight, height, width
            payload = {
                "pickupPincode":     int(pickup_pincode),
                "dropPincode":       int(drop_pincode),
                "weight":            weight_grams,
                "length":            30,   # default cm; callers can extend this API later
                "height":            10,
                "width":             20,
                "serviceType":       "SURFACE",        # SURFACE | EXPRESS (per account)
                "billingClientType": "SELLER",         # required by spec
                "shippingDirection": "FORWARD",        # FORWARD | REVERSE
                "invoiceAmount":     cod_amount or 0,  # spec: invoice value of goods
            }
            if payment_mode == "COD":
                payload["codAmount"] = cod_amount      # spec: codAmount (camelCase)

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    url,
                    json=payload,
                    headers=self._auth_headers(token),
                )
                resp.raise_for_status()
                return resp.json()

        except Exception as e:
            print(f"[EkartClient] get_shipping_estimate error: {e}")
            return {"error": str(e)}

    # ── Set Dispatch Date ─────────────────────────────────────────────────────
    # POST /data/shipment/dispatch-date
    # For delayed_dispatch=true shipments — set preferred pickup date

    async def set_dispatch_date(self, awb_numbers: list[str], dispatch_date: str) -> dict:
        """
        dispatch_date: "YYYY-MM-DD" format
        """
        if self.mock_mode:
            return {"mock": True, "ids": awb_numbers, "dispatchDate": dispatch_date}

        try:
            token = await self._get_token()
            url = f"{EKART_BASE_URL}/data/shipment/dispatch-date"

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    url,
                    json={"ids": awb_numbers, "dispatchDate": dispatch_date},
                    headers=self._auth_headers(token),
                )
                resp.raise_for_status()
                return resp.json()

        except Exception as e:
            err = self._extract_error(e)
            print(f"[EkartClient] set_dispatch_date error: {err}")
            return {"error": err}

    # ── COD Collection Report ─────────────────────────────────────────────────
    # GET /data/v1/elite/track/{wbn} → filter delivered COD shipments
    # Ekart does not expose a dedicated COD report endpoint; we build it from
    # our local DB + live track calls.

    # ── Register Pickup / Return Address ─────────────────────────────────────
    # POST /api/v2/address
    # Registers a warehouse address with Ekart so it can be referenced by alias.
    # Required: alias, phone (int), address_line1, pincode (int)
    # Response: { status: bool, alias: str, remark: str }
    # Call this ONCE per warehouse. Store the returned alias in
    # ShipmentConfigurations.PickupAlias so future shipments use alias-only format.

    async def register_address(
        self,
        alias: str,
        phone: str,
        address_line1: str,
        pincode: str,
        address_line2: Optional[str] = None,
        city: Optional[str] = None,
        state: Optional[str] = None,
        country: str = "India",
    ) -> dict:
        """
        Register a pickup/return warehouse address with Ekart.
        On success: { "status": true, "alias": "...", "remark": "..." }
        The alias is what you store in ShipmentConfigurations.PickupAlias.
        """
        if self.mock_mode:
            return {"status": True, "alias": alias, "remark": "Registered (mock mode)"}

        def _phone_int(p: str) -> int:
            digits = "".join(c for c in (p or "") if c.isdigit())
            return int(digits[-10:]) if len(digits) >= 10 else int(digits or "0")

        def _pin_int(p: str) -> int:
            digits = "".join(c for c in (p or "") if c.isdigit())
            return int(digits[:6]) if digits else 0

        try:
            token = await self._get_token()
            payload: dict = {
                "alias":        alias,
                "phone":        _phone_int(phone),
                "address_line1": address_line1,
                "pincode":      _pin_int(pincode),
            }
            if address_line2: payload["address_line2"] = address_line2
            if city:          payload["city"]          = city
            if state:         payload["state"]         = state
            if country:       payload["country"]       = country

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{EKART_BASE_URL}/api/v2/address",
                    json=payload,
                    headers=self._auth_headers(token),
                )
                resp.raise_for_status()
                return resp.json()

        except Exception as e:
            err = self._extract_error(e)
            print(f"[EkartClient] register_address error: {err}")
            return {"status": False, "alias": alias, "remark": err}

    # ── List Registered Addresses ─────────────────────────────────────────────
    # GET /api/v2/addresses
    # Returns all addresses registered against your Ekart account.

    async def list_addresses(self) -> list:
        """
        Returns all registered pickup/return addresses for this Ekart account.
        Each address has an alias that can be used in shipment creation.
        """
        if self.mock_mode:
            return [{"alias": "mock-warehouse", "mock": True}]

        try:
            token = await self._get_token()
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{EKART_BASE_URL}/api/v2/addresses",
                    headers=self._auth_headers(token),
                )
                resp.raise_for_status()
                return resp.json()

        except Exception as e:
            print(f"[EkartClient] list_addresses error: {e}")
            return []

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_error(e: Exception) -> str:
        if httpx and isinstance(e, httpx.HTTPStatusError):
            try:
                body = e.response.json()
                # Ekart error schema: {statusCode, code, message, description, severity}
                # Log the full body so we can diagnose SWIFT_VALIDATION_EXCEPTION etc.
                print(f"[EkartClient] Full error response (HTTP {e.response.status_code}): {body}")
                # Return combined message + description for maximum detail
                msg  = body.get("message") or ""
                desc = body.get("description") or ""
                code = body.get("code") or ""
                combined = " | ".join(filter(None, [msg, desc, code]))
                return combined or f"HTTP {e.response.status_code} (no message body)"
            except Exception:
                raw = e.response.text[:500]
                print(f"[EkartClient] Raw error body: {raw}")
                return f"HTTP {e.response.status_code}: {raw or '(empty response body)'}"

        # FIX: str(e) can be an empty string for some httpx network errors
        # (ConnectError, ConnectTimeout, ReadTimeout, etc) — never return "".
        # Always include the exception TYPE so we can diagnose, even when the
        # exception itself carries no message.
        msg = str(e).strip()
        exc_type = type(e).__name__
        if httpx and isinstance(e, httpx.RequestError):
            # Network-level failure before any response was received —
            # DNS failure, connection refused, timeout, TLS error, etc.
            url = getattr(getattr(e, "request", None), "url", "unknown URL")
            return f"{exc_type}: {msg or 'no message'} (request to {url} failed before response)"

        return f"{exc_type}: {msg}" if msg else f"{exc_type} (no error message available)"

    # ── Mock Responses ────────────────────────────────────────────────────────

    def _mock_create_shipment(self, req: CreateShipmentRequest) -> EkartShipmentResponse:
        import random
        awb = f"500999{random.randint(1000000, 9999999)}"
        return EkartShipmentResponse(
            success=True,
            awb_number=awb,
            tracking_url=f"{EKART_BASE_URL}/track/{awb}",
            courier_name="Ekart (mock)",
        )

    def _mock_create_return_shipment(self, req: CreateReturnShipmentRequest) -> EkartShipmentResponse:
        import random
        awb = f"500RTN{random.randint(1000000, 9999999)}"
        return EkartShipmentResponse(success=True, awb_number=awb, courier_name="Ekart (mock)")

    def _mock_track_shipment(self, awb: str) -> EkartTrackingResponse:
        now = datetime.now(timezone.utc)
        return EkartTrackingResponse(
            awb_number=awb,
            current_status="In Transit",
            current_status_code="ITR",
            expected_delivery=now,
            events=[
                EkartTrackingEvent(
                    status="Picked Up from Seller",
                    status_code="PKD",
                    location="Hyderabad Hub",
                    timestamp=now,
                    remarks="Package picked up",
                ),
                EkartTrackingEvent(
                    status="In Transit",
                    status_code="ITR",
                    location="Hyderabad Hub",
                    timestamp=now,
                    remarks="Package moving to destination",
                ),
            ],
        )


# ── Factory ───────────────────────────────────────────────────────────────────

def get_ekart_client_for_db(db) -> EkartClient:
    db_config = load_ekart_config_from_db(db)
    return EkartClient(db_config=db_config)


def map_ekart_status_to_order_state(ekart_status: str) -> str:
    return EKART_TO_ORDER_STATUS.get(ekart_status, "Shipped")


# ── Legacy singleton ──────────────────────────────────────────────────────────
_legacy_client: Optional[EkartClient] = None


def get_ekart_client() -> EkartClient:
    global _legacy_client
    if _legacy_client is None:
        _legacy_client = EkartClient(db_config=None)
    return _legacy_client