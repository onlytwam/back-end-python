"""
Auth routes — mirrors:
  IdentityServer4 /connect/token  → POST /auth/token
  RegisterController              → POST /auth/signup, /auth/update, etc.

Secret management change
─────────────────────────
oauth_client_id and oauth_client_secret are now validated against AppSettings
(the DB table) instead of being hardcoded strings in source code.

Sync guarantee
──────────────
Every write that touches auth.IdentityUser also mirrors the change to
twam.People (via _sync_people). Both tables stay in lockstep:
  • signup          → insert both rows atomically (in repository)
  • update profile  → handled in repository.update_identity_user_profile
  • updateUser      → syncs IsActive, RoleId, name, phone, deletedInd
  • deleteUser      → soft-deletes both rows in one transaction
"""

import os
import random
import re
import string
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Form, Request
from sqlalchemy.orm import Session
from sqlalchemy import text

from app import people
from database import get_db
from app.people.models import People
from .schemas import (
    TokenResponse, RegisterUserRequest,
    ResetPasswordRequest, ResetPasswordConfirmRequest,
    SignedInChangePasswordRequest,
)
from .repository import (
    authenticate_user, build_token_claims,
    create_identity_user,
    change_password_for_authenticated_user,
    _get_identity_user_by_username,
    _next_people_id,
    parse_profile_update_payload,
    update_identity_user_profile,
    reset_password_with_token,
    _MAX_FAILED_ATTEMPTS,
)
from .security import create_access_token, decode_reset_token, EXPIRES_MINUTES
from .dependencies import get_current_user, require_roles, Roles, CurrentUser

from typing import Optional as _Optional
from fastapi.security import HTTPBearer as _HTTPBearer, HTTPAuthorizationCredentials as _Creds
from app.auth.security import blacklist_token as _blacklist_token

# NOTE: WhatsApp welcome is fired inside repository.create_identity_user via
# _send_welcome_notifications(). Do NOT import or call send_whatsapp here —
# it would crash on startup (wrong module path) AND send the message twice.

router = APIRouter(prefix="/api/auth", tags=["Authentication"])

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:4200")

_OTP_TTL_MINUTES = 60
_OTP_TABLE = 'auth."OtpStore"'


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _validate_client(client_id: Optional[str], client_secret: Optional[str]) -> bool:
    from app.shared.app_settings import get_setting
    valid_id     = get_setting("oauth_client_id")
    valid_secret = get_setting("oauth_client_secret")
    return (client_id or "") == valid_id and (client_secret or "") == valid_secret


def _mask_email(email: str) -> str:
    if not email or "@" not in email:
        return email
    local, domain = email.split("@", 1)
    return local[:2] + "***@" + domain


def _mask_phone(phone: str) -> str:
    if not phone or len(phone) < 4:
        return phone
    return phone[:2] + "******" + phone[-2:]


def _gen_otp(length: int = 6) -> str:
    return "".join(random.choices(string.digits, k=length))


def _is_email_identifier(identifier: str) -> bool:
    return "@" in (identifier or "")


def _normalize_identifier(identifier: str) -> str:
    return (identifier or "").strip()


def _unique_identifiers(*values: Optional[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        item = _normalize_identifier(value)
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _cleanup_expired_otps(db: Session) -> None:
    db.execute(text(f'DELETE FROM {_OTP_TABLE} WHERE "ExpiresAt" < NOW()'))
    db.commit()


def _db_otp_store(db: Session, identifier: str, otp: str, purpose: str) -> None:
    """Store or refresh an OTP. identifier may be email or phone number."""
    identifier = _normalize_identifier(identifier)
    if not identifier:
        return

    _cleanup_expired_otps(db)
    identifier_type = "email" if _is_email_identifier(identifier) else "phone"
    expires = datetime.now(timezone.utc) + timedelta(minutes=_OTP_TTL_MINUTES)
    db.execute(text(f"""
        INSERT INTO {_OTP_TABLE}
            ("Identifier", "IdentifierType", "Otp", "ExpiresAt", "Purpose")
        VALUES
            (:identifier, :id_type, :otp, :expires, :purpose)
        ON CONFLICT ("Identifier", "Purpose") DO UPDATE
            SET "Otp"       = EXCLUDED."Otp",
                "ExpiresAt" = EXCLUDED."ExpiresAt"
    """), {
        "identifier": identifier,
        "id_type":    identifier_type,
        "otp":        otp,
        "expires":    expires,
        "purpose":    purpose,
    })
    db.commit()


def _db_otp_get(db: Session, identifier: str, purpose: Optional[str] = None) -> dict | None:
    identifier = _normalize_identifier(identifier)
    if not identifier:
        return None

    if purpose:
        row = db.execute(text(f"""
            SELECT "Otp", "ExpiresAt", "Purpose", "IdentifierType"
            FROM   {_OTP_TABLE}
            WHERE  "Identifier" = :identifier AND "Purpose" = :purpose
            ORDER BY "ExpiresAt" DESC LIMIT 1
        """), {"identifier": identifier, "purpose": purpose}).fetchone()
    else:
        row = db.execute(text(f"""
            SELECT "Otp", "ExpiresAt", "Purpose", "IdentifierType"
            FROM   {_OTP_TABLE}
            WHERE  "Identifier" = :identifier
            ORDER BY "ExpiresAt" DESC LIMIT 1
        """), {"identifier": identifier}).fetchone()
    if not row:
        return None
    return {"otp": row[0], "expires_at": row[1], "purpose": row[2], "identifier_type": row[3]}


def _db_otp_delete(db: Session, identifier: str, purpose: Optional[str] = None) -> None:
    identifier = _normalize_identifier(identifier)
    if not identifier:
        return

    if purpose:
        db.execute(text(f"""
            DELETE FROM {_OTP_TABLE}
            WHERE "Identifier" = :identifier AND "Purpose" = :purpose
        """), {"identifier": identifier, "purpose": purpose})
    else:
        db.execute(text(f'DELETE FROM {_OTP_TABLE} WHERE "Identifier" = :identifier'),
                   {"identifier": identifier})
    db.commit()


def _store_otp_for_user(db: Session, user, otp: str, purpose: str, *, identifier: Optional[str] = None) -> None:
    identifiers = _unique_identifiers(identifier, user.PhoneNumber, user.Email)
    for value in identifiers:
        _db_otp_store(db, value, otp, purpose)


def _send_phone_otp(db: Session, phone: str, otp: str, channel: str) -> bool:
    """
    Send OTP via WhatsApp or SMS.

    For SMS: calls _send_sms with the correct named_vars + positional_order
    matching the OTP template:
        DB placeholder : {otp}
        SmartPing var  : var1 = otp
    """
    channel = (channel or "").strip().lower()

    if channel == "whatsapp":
        try:
            from app.whatsapp.service import send_whatsapp
            return send_whatsapp(
                db,
                phone,
                (
                    "Your TWAM login OTP is "
                    f"{otp}. It is valid for {_OTP_TTL_MINUTES} minutes."
                ),
                "OTP",
            )
        except Exception as e:
            print(f"[Auth] OTP WhatsApp failed: {e}")
            return False

    # SMS path — must match _send_sms(db, phone, message_type, named_vars, positional_order)
    try:
        from app.ekart.notifications import _send_sms
        return _send_sms(
            db, phone, "OTP",
            named_vars={"otp": otp},
            positional_order=["otp"],
        )
    except Exception as e:
        print(f"[Auth] OTP SMS failed: {e}")
        return False


def _send_email_otp(db: Session, email: str, otp: str, *, full_name: str = "") -> bool:
    try:
        from app.sendemail.repository import send_email  # noqa: F401 (also imported inline)
        from app.sendemail.schemas import EmailCreate
        name = full_name.strip() or email
        from app.shared.email_template import twam_email, greeting
        content = (
            greeting(name)
            + f'''<p style="margin:0 0 24px;">Your one-time login code is:</p>
              <table width="100%" cellpadding="0" cellspacing="0"><tr>
                <td align="center" style="padding:8px 0 28px;">
                  <div style="display:inline-block;background:#F6EEE1;
                               border:2px dashed #9B7080;border-radius:4px;
                               padding:20px 40px;text-align:center;">
                    <p style="margin:0 0 6px;font-size:9pt;color:#9B7080;
                               letter-spacing:0.20em;text-transform:uppercase;">
                      Your OTP Code</p>
                    <p style="margin:0;font-size:36px;font-weight:700;
                               letter-spacing:0.20em;color:#6A012A;">{otp}</p>
                  </div>
                </td>
              </tr></table>
              <p style="margin:0;font-size:12px;color:#9B7080;">
                Valid for {_OTP_TTL_MINUTES} minutes. Do not share this code with anyone.</p>'''
        )
        return send_email(db, EmailCreate(
            name    = name,
            email   = email,
            subject = "Your Login OTP — TWAM",
            message = twam_email(title="Your Login OTP — TWAM", content=content),
            filename=None, isFile=False, data=None,
        ))
    except Exception as e:
        print(f"[Auth] OTP email failed: {e}")
        return False

def _send_otp_for_user(
    db: Session,
    user,
    *,
    purpose: str,
    channel: str = "email",
    identifier: Optional[str] = None,
) -> dict:

    otp = _gen_otp()
    channel = (channel or "email").strip().lower()

    has_phone = bool(user.PhoneNumber and user.PhoneNumber.strip())
    has_email = bool(user.Email and "@" in user.Email)

    sent_email = False
    sent_phone = False

    # EMAIL
    if channel == "email" and has_email:

        sent_email = _send_email_otp(
            db,
            user.Email,
            otp,
            full_name=user.full_name or user.Email
        )

        if sent_email:
            _store_otp_for_user(
                db,
                user,
                otp,
                purpose,
                identifier=identifier
            )

    # SMS / WHATSAPP
    elif channel in {"whatsapp", "sms"} and has_phone:

        sent_phone = _send_phone_otp(
            db,
            user.PhoneNumber,
            otp,
            channel
        )

        if sent_phone:
            _store_otp_for_user(
                db,
                user,
                otp,
                purpose,
                identifier=identifier
            )

    # FALLBACK EMAIL
    elif has_email:

        sent_email = _send_email_otp(
            db,
            user.Email,
            otp,
            full_name=user.full_name or user.Email
        )

        if sent_email:
            _store_otp_for_user(
                db,
                user,
                otp,
                purpose,
                identifier=identifier
            )

    # FALLBACK SMS
    elif has_phone:

        sent_phone = _send_phone_otp(
            db,
            user.PhoneNumber,
            otp,
            "sms"
        )

        if sent_phone:
            _store_otp_for_user(
                db,
                user,
                otp,
                purpose,
                identifier=identifier
            )

    # IMPORTANT
    if not sent_email and not sent_phone:
        raise HTTPException(
            status_code=500,
            detail="Failed to send OTP."
        )

    return {
        "registered": True,
        "hasEmail": has_email,
        "hasPhone": has_phone,
        "channels": (
            ["email"] if has_email and not has_phone else
            ["whatsapp", "sms"] if has_phone and not has_email else
            ["email", "whatsapp", "sms"]
        ),
        "selectedChannel": channel,
        "maskedPhone": _mask_phone(user.PhoneNumber) if has_phone else None,
        "maskedEmail": _mask_email(user.Email) if has_email else None,
        "sentEmail": sent_email,
        "sentPhone": sent_phone,
        "message": "OTP sent.",
    }

def _registration_conflict_message() -> str:
    return "This email or phone number is already registered. Please sign in instead."


def _validate_signup_contact(request: RegisterUserRequest) -> None:
    email = _normalize_identifier(request.EmailId or "")
    phone = _normalize_identifier(request.PhoneNumber or "")

    if not email:
        raise HTTPException(status_code=400, detail={"message": "Email is required."})
    if not phone:
        raise HTTPException(status_code=400, detail={"message": "Phone number is required."})
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=400, detail={"message": "Invalid email address."})
    if not re.fullmatch(r"\d{10}", phone):
        raise HTTPException(status_code=400, detail={"message": "10-digit phone number required."})

    request.EmailId = email
    request.PhoneNumber = phone


def _has_duplicate_registration_error(errors: list[dict]) -> bool:
    return any(
        (error or {}).get("code") in {"DuplicatePhoneNumber", "DuplicateEmail"}
        for error in errors
    )


def _sync_people(db: Session, user, *, modified_by: str = "system") -> None:
    """
    Mirror every user-facing field from auth.IdentityUser → twam.People.
    Called after every admin write (updateUser, deleteUser).
    Keeps both tables in the same transaction so a sync failure aborts the write.
    """
    try:
        people = db.query(People).filter(People.userProfileId == user.Id).first()
        if people is None:
            people_id = _next_people_id(db)
            if people_id is None:
                raise RuntimeError("Could not allocate People.PersonalId.")
            people = People(
                personalId    = people_id,
                userProfileId = user.Id,
                createdDate   = datetime.now(timezone.utc),
                createdBy     = user.CreatedBy or modified_by,
                organizationId= user.OrganizationId,
            )
            db.add(people)

        people.firstName       = user.FirstName
        people.middleName      = user.MiddleName
        people.lastName        = user.LastName
        people.emailId         = user.Email
        people.phoneNumber     = user.PhoneNumber
        people.profileImageUrl = user.ProfileImageUrl
        people.isActive        = user.IsActive
        people.roleId          = user.UserRoleId
        people.deletedInd      = user.DeletedInd
        people.modifiedDate    = datetime.now(timezone.utc)
        people.modifiedBy      = modified_by
        db.flush()
    except Exception as e:
        print(f"[Sync] People sync failed for user {user.Id}: {e}")
        raise


def _send_otp_to_channels(db: Session, user, *, purpose: str) -> dict:
    """
    Generate one OTP and dispatch it to every available channel (SMS + email).
    OTP is stored keyed by both phone and email so either can be used to verify.

    SMS call matches _send_sms(db, phone, message_type, named_vars, positional_order):
        named_vars      = {"otp": otp}
        positional_order= ["otp"]          → var1 = otp value sent to SmartPing
    """
    otp       = _gen_otp()
    has_phone = bool(user.PhoneNumber and user.PhoneNumber.strip())
    has_email = bool(user.Email and "@" in user.Email)

    if has_phone:
        _db_otp_store(db, user.PhoneNumber, otp, purpose)
    if has_email:
        _db_otp_store(db, user.Email, otp, purpose)

    # ── SMS ──────────────────────────────────────────────────────────────────
    if has_phone:
        try:
            from app.ekart.notifications import _send_sms
            _send_sms(
                db, user.PhoneNumber, "OTP",
                named_vars={"otp": otp},
                positional_order=["otp"],
            )
        except Exception as e:
            print(f"[Auth] OTP SMS failed: {e}")

    # ── Email ─────────────────────────────────────────────────────────────────
    if has_email:
        try:
            from app.sendemail.repository import send_email
            from app.sendemail.schemas import EmailCreate
            name = user.full_name or user.Email
            from app.shared.email_template import twam_email, greeting
            _otp_content = (
                greeting(name)
                + f'''<p style="margin:0 0 24px;">Your one-time login code is:</p>
                  <table width="100%" cellpadding="0" cellspacing="0"><tr>
                    <td align="center" style="padding:8px 0 28px;">
                      <div style="display:inline-block;background:#F6EEE1;
                                   border:2px dashed #9B7080;border-radius:4px;
                                   padding:20px 40px;text-align:center;">
                        <p style="margin:0 0 6px;font-size:9pt;color:#9B7080;
                                   letter-spacing:0.20em;text-transform:uppercase;">
                          Your OTP Code</p>
                        <p style="margin:0;font-size:36px;font-weight:700;
                                   letter-spacing:0.20em;color:#6A012A;">{otp}</p>
                      </div>
                    </td>
                  </tr></table>
                  <p style="margin:0;font-size:12px;color:#9B7080;">
                    Valid for {_OTP_TTL_MINUTES} minutes. Do not share this code with anyone.</p>'''
            )
            send_email(db, EmailCreate(
                name    = name,
                email   = user.Email,
                subject = "Your Login OTP — TWAM",
                message = twam_email(title="Your Login OTP — TWAM", content=_otp_content),
                filename=None, isFile=False, data=None,
            ))
        except Exception as e:
            print(f"[Auth] OTP email failed: {e}")

    return {
        "hasPhone":    has_phone,
        "hasEmail":    has_email,
        "maskedPhone": _mask_phone(user.PhoneNumber) if has_phone else None,
        "maskedEmail": _mask_email(user.Email)       if has_email else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# TOKEN — POST /connect/token
# ─────────────────────────────────────────────────────────────────────────────

@router.post("connect/token", response_model=TokenResponse, summary="Login — get JWT access token")
async def token(
    grant_type:    str           = Form(...),
    username:      str           = Form(...),
    password:      str           = Form(...),
    client_id:     Optional[str] = Form(default=None),
    client_secret: Optional[str] = Form(default=None),
    scope:         Optional[str] = Form(default=None),
    db:            Session       = Depends(get_db),
):
    if grant_type != "password":
        raise HTTPException(status_code=400, detail="unsupported_grant_type")
    if not _validate_client(client_id, client_secret):
        raise HTTPException(status_code=401, detail="invalid_client")

    result = authenticate_user(db, username, password)
    if not result.success:
        if result.error == "locked_out" and result.lockout_remaining_minutes:
            raise HTTPException(status_code=400, detail={
                "error":              "locked_out",
                "message":            f"Account is temporarily locked. Try again in {result.lockout_remaining_minutes} minute(s).",
                "failed_count":       _MAX_FAILED_ATTEMPTS,
                "max_attempts":       _MAX_FAILED_ATTEMPTS,
            })
        elif result.error == "deactivated":
            raise HTTPException(status_code=400, detail={
                "error":              "deactivated",
                "message":            "Account is deactivated.",
                "failed_count":       0,
                "max_attempts":       _MAX_FAILED_ATTEMPTS,
            })
        elif result.error == "locked_out":
            raise HTTPException(status_code=400, detail={
                "error":              "locked_out",
                "message":            "Account is locked. Please reset your password.",
                "failed_count":       _MAX_FAILED_ATTEMPTS,
                "max_attempts":       _MAX_FAILED_ATTEMPTS,
            })
        elif result.error == "invalid_credentials":
            raise HTTPException(status_code=400, detail={
                "error":              "invalid_credentials",
                "message":            "Invalid username or password.",
                "failed_count":       result.failed_count or 1,
                "max_attempts":       _MAX_FAILED_ATTEMPTS,
            })
        else:
            raise HTTPException(status_code=400, detail={
                "error":              result.error,
                "message":            "Authentication failed.",
                "failed_count":       0,
                "max_attempts":       _MAX_FAILED_ATTEMPTS,
            })

    # Build claims first — they already contain storeId, warehouseId, warehouseName
    # resolved from the People table. Use them directly instead of re-reading the
    # IdentityUser relationship (which had the warehouseId attribute-name bug).
    claims       = build_token_claims(db, result.user)
    access_token = create_access_token(claims)

    return TokenResponse(
        access_token  = access_token,
        expires_in    = EXPIRES_MINUTES * 60,
        storeId       = claims.get("storeId"),
        warehouseId   = claims.get("warehouseId"),
        warehouseName = claims.get("warehouseName", ""),
    )


# ─────────────────────────────────────────────────────────────────────────────
# SIGNUP — POST /auth/signup
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/signup", status_code=201, summary="Register a new user")
async def signup(request: RegisterUserRequest, db: Session = Depends(get_db)):
    _validate_signup_contact(request)
    result = create_identity_user(db, request)

    if result["succeeded"]:
        return {"message": "User created successfully.", "userId": result["user_id"]}
    errors = result.get("errors", [])
    if _has_duplicate_registration_error(errors):
        raise HTTPException(
            status_code=409,
            detail={
                "already_registered": True,
                "message": _registration_conflict_message(),
            },
        )
    raise HTTPException(status_code=400, detail={"message": "User creation failed.", "errors": errors})


# ─────────────────────────────────────────────────────────────────────────────
# PHONE / EMAIL OTP LOGIN
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/send-login-otp", summary="Send OTP to phone or email for passwordless login")
async def send_login_otp(payload: dict, db: Session = Depends(get_db)):
    """
    Body: { "identifier": "9876543210" }   ← 10-digit phone OR email
    Sends OTP to all channels the account has (SMS + email).
    Always returns 200 — never reveals whether the account exists.
    """
    identifier = _normalize_identifier(
        payload.get("identifier") or payload.get("email") or payload.get("phone")
    )
    channel = _normalize_identifier(payload.get("channel") or "")
    if not identifier:
        raise HTTPException(status_code=400, detail="identifier is required.")

    user = _get_identity_user_by_username(db, identifier)
    if not user or user.DeletedInd or not user.IsActive:
        return {
            "success":  True,
            "message":  "If this account exists, an OTP has been sent.",
            "hasPhone": False,
            "hasEmail": False,
        }

    if _is_email_identifier(identifier):
        channel = "email"
    elif channel.lower() not in {"whatsapp", "sms"}:
        channel = "whatsapp" if user.PhoneNumber else "email"

    channels = _send_otp_for_user(db, user, purpose="phone_login", channel=channel, identifier=identifier)
    return {"success": True, "message": "OTP sent. Please check your phone or email.", **channels}


@router.post("/verify-login-otp", summary="Verify OTP and return JWT token")
async def verify_login_otp(payload: dict, db: Session = Depends(get_db)):
    """
    Body: { "identifier": "9876543210", "otp": "123456" }
    identifier = phone or email. Returns same JWT shape as password login.
    """
    identifier = _normalize_identifier(
        payload.get("identifier") or payload.get("email") or payload.get("phone")
    )
    otp = (payload.get("otp") or "").strip()

    if not identifier or not otp:
        raise HTTPException(status_code=400, detail="identifier and otp are required.")

    stored = _db_otp_get(db, identifier, purpose="phone_login")
    if not stored:
        user = _get_identity_user_by_username(db, identifier)
        if user:
            for alt_identifier in _unique_identifiers(identifier, user.Email, user.PhoneNumber):
                stored = _db_otp_get(db, alt_identifier, purpose="phone_login")
                if stored:
                    identifier = alt_identifier
                    break
    if not stored:
        raise HTTPException(status_code=400, detail="OTP not found. Please request a new one.")
    if stored["otp"] != otp:
        raise HTTPException(status_code=400, detail="Invalid OTP.")

    expires_at = stored["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        user = _get_identity_user_by_username(db, identifier)
        for alt_identifier in _unique_identifiers(
            identifier,
            user.Email        if user else None,
            user.PhoneNumber  if user else None,
        ):
            _db_otp_delete(db, alt_identifier, purpose="phone_login")
        raise HTTPException(status_code=400, detail="OTP has expired. Please request a new one.")

    user = _get_identity_user_by_username(db, identifier)
    if not user or user.DeletedInd or not user.IsActive:
        raise HTTPException(status_code=404, detail="Account not found or inactive.")

    for ident in {identifier, user.PhoneNumber or "", user.Email or ""}:
        if ident:
            _db_otp_delete(db, ident, purpose="phone_login")

    claims       = build_token_claims(db, user)
    access_token = create_access_token(claims)
    return {
        "success":      True,
        "access_token": access_token,
        "token_type":   "Bearer",
        "expires_in":   EXPIRES_MINUTES * 60,
        "message":      "Login successful.",
    }


# ─────────────────────────────────────────────────────────────────────────────
# UPDATE PROFILE — POST /auth/update
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/update", summary="Update current user profile (requires login)")
async def update_profile(
    request:      Request,
    current_user: CurrentUser = Depends(get_current_user),
    db:           Session     = Depends(get_db),
):
    payload, profile_image = await parse_profile_update_payload(request)
    return update_identity_user_profile(
        db=db,
        user_id=current_user.user_id,
        request=payload,
        profile_image=profile_image,
    )


# ─────────────────────────────────────────────────────────────────────────────
# UPDATE USER (admin) — POST /auth/updateUser
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/updateUser", summary="Admin: update user role / active status")
async def update_user(
    request:      RegisterUserRequest,
    current_user: CurrentUser = Depends(require_roles(Roles.SUPER_ADMIN)),
    db:           Session     = Depends(get_db),
):
    if not request.EmailId:
        raise HTTPException(status_code=400, detail="EmailId is required.")

    user = _get_identity_user_by_username(db, request.EmailId)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    user.IsActive          = request.IsActive
    user.UserRoleId        = request.RoleId
    user.AccessFailedCount = 0
    user.LockoutEnd        = None
    user.ModifiedDate      = datetime.now(timezone.utc)
    user.ModifiedBy        = current_user.email
    db.flush()

    _sync_people(db, user, modified_by=current_user.email)
    db.commit()

    return True


# ─────────────────────────────────────────────────────────────────────────────
# DELETE USER (admin) — POST /auth/deleteUser
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/deleteUser", summary="Admin: soft-delete a user from both tables")
async def delete_user(
    payload:      dict,
    current_user: CurrentUser = Depends(require_roles(Roles.SUPER_ADMIN)),
    db:           Session     = Depends(get_db),
):
    """
    Body: { "emailId": "user@example.com" }
    Sets DeletedInd=True, IsActive=False on both tables in one transaction.
    """
    email = (payload.get("emailId") or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="emailId is required.")

    user = _get_identity_user_by_username(db, email)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    if user.DeletedInd:
        raise HTTPException(status_code=400, detail="User is already deleted.")

    now = datetime.now(timezone.utc)

    user.DeletedInd   = True
    user.IsActive     = False
    user.ModifiedDate = now
    user.ModifiedBy   = current_user.email
    _sync_people(db, user, modified_by=current_user.email)

    db.commit()
    return {"success": True, "message": f"User {email} has been deleted."}


# ─────────────────────────────────────────────────────────────────────────────
# CHANGE PASSWORD — POST /auth/ChangePassword
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/ChangePassword", summary="Change password (requires login)")
async def change_password_endpoint(
    request:      SignedInChangePasswordRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db:           Session     = Depends(get_db),
):
    if not current_user.email:
        raise HTTPException(status_code=400, detail="Authenticated user email is required.")

    result = change_password_for_authenticated_user(db, current_user.email, request.NewPassword)
    if result["succeeded"]:
        return {"message": "Password has been changed successfully."}
    raise HTTPException(status_code=400, detail={"errors": result["errors"]})


# ─────────────────────────────────────────────────────────────────────────────
# FORGOT PASSWORD — POST /auth/SendResetPasswordLink
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/SendResetPasswordLink", summary="Send password reset email")
async def send_reset_password(request: ResetPasswordRequest, db: Session = Depends(get_db)):
    generic_response = {"message": "If this email is registered, a reset link has been sent."}

    user = _get_identity_user_by_username(db, request.EmailId)
    if not user:
        return generic_response

    reset_token = create_access_token(
        user_claims={"sub": user.Email, "purpose": "password_reset", "email": user.Email},
        expires_minutes=15,
    )
    reset_link = f"{FRONTEND_URL}/reset-password?token={reset_token}"

    try:
        from app.sendemail.repository import send_email
        from app.sendemail.schemas import EmailCreate
        full_name = user.full_name or user.Email
        from app.shared.email_template import twam_email, greeting, cta_button
        _reset_content = (
            greeting(full_name)
            + '''<p style="margin:0 0 20px;">We received a request to reset your password.
              Click the button below to choose a new one. This link is valid for
              <strong>15 minutes</strong>.</p>'''
            + cta_button("Reset My Password", reset_link)
            + '''<p style="margin:16px 0 0;font-size:12px;color:#9B7080;text-align:center;">
              If you did not request this, you can safely ignore this email.</p>'''
        )
        sent = send_email(db, EmailCreate(
            name    = full_name,
            email   = user.Email,
            subject = request.Subject or "Reset Your Password — Only TWAM",
            message = twam_email(title="Reset Your Password — TWAM", content=_reset_content),
            filename=None, isFile=False, data=None,
        ))
        if not sent:
            raise HTTPException(status_code=500, detail="Failed to send reset email.")
    except ImportError:
        raise HTTPException(status_code=500, detail="Email service not available.")

    return generic_response


# ─────────────────────────────────────────────────────────────────────────────
# RESET PASSWORD — POST /auth/ResetPassword
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/ResetPassword", summary="Reset password using token from email link")
async def reset_password(request: ResetPasswordConfirmRequest, db: Session = Depends(get_db)):
    try:
        payload = decode_reset_token(request.Token)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token.")

    if payload.get("purpose") != "password_reset":
        raise HTTPException(status_code=400, detail="Invalid reset token.")

    email = payload.get("email") or payload.get("sub")
    if not email:
        raise HTTPException(status_code=400, detail="Invalid reset token.")

    result = reset_password_with_token(db, email, request.NewPassword)
    if not result["succeeded"]:
        raise HTTPException(status_code=400, detail={"errors": result["errors"]})

    return {"message": "Password has been reset successfully. You can now log in."}


# ─────────────────────────────────────────────────────────────────────────────
# ME — GET /auth/me
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/me", response_model=CurrentUser, summary="Get current user info from token")
async def me(current_user: CurrentUser = Depends(get_current_user)):
    return current_user



# ─────────────────────────────────────────────────────────────────────────────
# FORGOT PASSWORD — 3-step OTP flow
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/forgot-password/check", summary="Check what channels the account has")
async def forgot_password_check(payload: dict, db: Session = Depends(get_db)):
    identifier = _normalize_identifier(
        payload.get("identifier") or payload.get("email") or payload.get("phone")
    )
    if not identifier:
        raise HTTPException(status_code=400, detail="Identifier is required.")

    user = _get_identity_user_by_username(db, identifier)
    if not user:
        return {
            "registered": False, "hasEmail": False, "hasPhone": False,
            "maskedEmail": None,  "maskedPhone": None, "channels": [],
        }

    has_email = bool(user.Email)
    has_phone = bool(user.PhoneNumber)
    channels = (
        ["email"]           if has_email and not has_phone else
        ["whatsapp", "sms"] if has_phone and not has_email else
        ["email", "whatsapp", "sms"]
    )
    return {
        "registered":  True,
        "hasEmail":    has_email,
        "hasPhone":    has_phone,
        "maskedEmail": _mask_email(user.Email)       if has_email else None,
        "maskedPhone": _mask_phone(user.PhoneNumber) if has_phone else None,
        "channels":    channels,
    }


@router.post("/forgot-password/send-otp", summary="Send OTP to chosen channel for password reset")
async def forgot_password_send_otp(payload: dict, db: Session = Depends(get_db)):
    identifier = _normalize_identifier(
        payload.get("identifier") or payload.get("email") or payload.get("phone")
    )
    channel = _normalize_identifier(payload.get("channel") or "")

    if not identifier:
        raise HTTPException(status_code=400, detail="Identifier is required.")

    user = _get_identity_user_by_username(db, identifier)
    if not user:
        return {"success": True, "message": "If this account exists, an OTP has been sent."}

    if _is_email_identifier(identifier):
        channel = "email"
    elif channel.lower() not in {"whatsapp", "sms"}:
        # channel = "whatsapp" if user.PhoneNumber else "email"
        channel = "sms" if user.PhoneNumber else "email"

    response = _send_otp_for_user(
        db, user, purpose="forgot_password", channel=channel, identifier=identifier
    )
    return {
        "success":         True,
        "message":         "OTP sent.",
        "selectedChannel": response["selectedChannel"],
        "hasEmail":        response["hasEmail"],
        "hasPhone":        response["hasPhone"],
        "maskedEmail":     response["maskedEmail"],
        "maskedPhone":     response["maskedPhone"],
        "channels":        response["channels"],
    }


@router.post("/forgot-password/verify-otp", summary="Verify OTP and reset password")
async def forgot_password_verify_otp(payload: dict, db: Session = Depends(get_db)):
    identifier   = _normalize_identifier(
        payload.get("identifier") or payload.get("email") or payload.get("phone")
    )
    otp          = (payload.get("otp")         or "").strip()
    new_password = (payload.get("newPassword") or "").strip()

    if not identifier or not otp or not new_password:
        raise HTTPException(
            status_code=400,
            detail="identifier, otp and newPassword are required.",
        )

    stored = _db_otp_get(db, identifier, purpose="forgot_password")
    if not stored:
        user = _get_identity_user_by_username(db, identifier)
        if user:
            for alt_identifier in _unique_identifiers(identifier, user.Email, user.PhoneNumber):
                stored = _db_otp_get(db, alt_identifier, purpose="forgot_password")
                if stored:
                    identifier = alt_identifier
                    break
    if not stored:
        raise HTTPException(status_code=400, detail="OTP not found. Please request a new one.")
    if stored["otp"] != otp:
        raise HTTPException(status_code=400, detail="Invalid OTP.")

    expires_at = stored["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        user = _get_identity_user_by_username(db, identifier)
        for alt_identifier in _unique_identifiers(
            identifier,
            user.Email       if user else None,
            user.PhoneNumber if user else None,
        ):
            _db_otp_delete(db, alt_identifier, purpose="forgot_password")
        raise HTTPException(status_code=400, detail="OTP has expired. Please request a new one.")

    user = _get_identity_user_by_username(db, identifier)
    if not user:
        raise HTTPException(status_code=404, detail="Account not found.")

    result = reset_password_with_token(
        db, user.Email or user.PhoneNumber or identifier, new_password
    )
    if not result["succeeded"]:
        raise HTTPException(status_code=400, detail={"errors": result["errors"]})

    for alt_identifier in _unique_identifiers(identifier, user.Email, user.PhoneNumber):
        _db_otp_delete(db, alt_identifier, purpose="forgot_password")

    return {"success": True, "message": "Password has been reset successfully. You can now sign in."}



# ─────────────────────────────────────────────────────────────────────────────
# LOGOUT — POST /auth/logout
# ─────────────────────────────────────────────────────────────────────────────

_bearer_scheme = _HTTPBearer(auto_error=False)


@router.post("/logout", summary="Logout — invalidate JWT token")
async def logout(
    credentials:  _Optional[_Creds] = Depends(_bearer_scheme),
    current_user: CurrentUser       = Depends(get_current_user),
    db:           Session           = Depends(get_db),
):
    if credentials and credentials.credentials:
        _blacklist_token(token=credentials.credentials, email=current_user.email, db=db)
    return {"success": True, "message": "Logged out successfully."}
