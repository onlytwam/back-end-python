"""
Auth routes — mirrors:
  IdentityServer4 /connect/token  → POST /auth/token
  RegisterController              → POST /auth/signup, /auth/update, etc.

Secret management change
─────────────────────────
oauth_client_id and oauth_client_secret are now validated against AppSettings
(the DB table) instead of being hardcoded strings in source code.
"""

import os
import random
import string
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, Form, Request
from sqlalchemy.orm import Session
from sqlalchemy import text

from database import get_db
from .schemas import (
    TokenResponse, RegisterUserRequest,
    ChangePasswordRequest, ResetPasswordRequest, ResetPasswordConfirmRequest,
)
from .repository import (
    authenticate_user, build_token_claims,
    create_identity_user, change_password,
    _get_identity_user_by_username,
    parse_profile_update_payload,
    update_identity_user_profile,
    reset_password_with_token,
)
from .security import create_access_token, decode_token, decode_reset_token, EXPIRES_MINUTES
from .dependencies import get_current_user, require_roles, Roles, CurrentUser

from typing import Optional as _Optional
from fastapi.security import HTTPBearer as _HTTPBearer, HTTPAuthorizationCredentials as _Creds
from app.auth.security import blacklist_token as _blacklist_token

router = APIRouter(prefix="/api/auth", tags=["Authentication"])

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:4200")


# ── OAuth2 client validation (reads from AppSettings, not hardcoded) ──────────

def _validate_client(client_id: Optional[str], client_secret: Optional[str]) -> bool:
    from app.shared.app_settings import get_setting
    valid_id     = get_setting("oauth_client_id")
    valid_secret = get_setting("oauth_client_secret")
    return (client_id or "") == valid_id and (client_secret or "") == valid_secret


# ─────────────────────────────────────────────────────────────────────────────
# TOKEN — POST /auth/token
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
        error_map = {
            "not_allowed":         "User not found.",
            "locked_out":          "Account is locked. Please reset your password.",
            "invalid_credentials": "Invalid username or password.",
        }
        raise HTTPException(status_code=400, detail=error_map.get(result.error, "Authentication failed."))

    claims       = build_token_claims(db, result.user)
    access_token = create_access_token(claims)

    return TokenResponse(access_token=access_token, expires_in=EXPIRES_MINUTES * 60)


# ─────────────────────────────────────────────────────────────────────────────
# SIGNUP — POST /auth/signup
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/signup", status_code=201, summary="Register a new user")
async def signup(request: RegisterUserRequest, db: Session = Depends(get_db)):
    result = create_identity_user(db, request)
    if result["succeeded"]:
        return {"message": "User created successfully.", "userId": result["user_id"]}
    raise HTTPException(status_code=400, detail={"errors": result["errors"]})


@router.post("/guest-signup", status_code=201, summary="Register a guest/user-role account")
async def guest_signup(request: RegisterUserRequest, db: Session = Depends(get_db)):
    request.RoleId = request.RoleId or Roles.USER
    result = create_identity_user(db, request)
    if result["succeeded"]:
        return {"Succeeded": True, "message": "User created successfully.", "userId": result["user_id"]}
    raise HTTPException(status_code=400, detail={"message": "User creation failed.", "errors": result["errors"]})


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

    user.IsActive = request.IsActive
    user.UserRoleId = request.RoleId
    user.AccessFailedCount = 0
    user.LockoutEnd = None
    user.ModifiedDate = datetime.now(timezone.utc)

    db.execute(
        text("""
            UPDATE twam."People"
            SET "IsActive" = :active, "RoleId" = :role, "ModifiedDate" = NOW()
            WHERE "UserProfileId" = :uid
        """),
        {"active": request.IsActive, "role": request.RoleId, "uid": user.Id},
    )
    db.commit()
    return True


# ─────────────────────────────────────────────────────────────────────────────
# CHANGE PASSWORD
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/ChangePassword", summary="Change password (requires login)")
async def change_password_endpoint(
    request:      ChangePasswordRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db:           Session     = Depends(get_db),
):
    if not request.EmailId:
        raise HTTPException(status_code=400, detail="EmailId is required.")

    result = change_password(db, request.EmailId, request.CurrentPassword, request.NewPassword)
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
        user_claims={
            "sub":     user.Email,
            "purpose": "password_reset",
            "email":   user.Email,
        },
        expires_minutes=15,
    )
    reset_link = f"{FRONTEND_URL}/reset-password?token={reset_token}"

    try:
        from app.sendemail.repository import send_email
        from app.sendemail.schemas import EmailCreate

        full_name = user.full_name or user.Email
        sent = send_email(db, EmailCreate(
            name    = full_name,
            email   = user.Email,
            subject = request.Subject or "Reset Your Password — Only TWAM",
            message = (
                f"Hello {full_name},\n\n"
                f"We received a request to reset your password.\n\n"
                f"Click the link below to reset it (valid for 15 minutes):\n"
                f"{reset_link}\n\n"
                f"If you did not request this, please ignore this email.\n\n"
                f"Regards,\nTWAM Team"
            ),
            filename=None, isFile=False, data=None,
        ))
        if not sent:
            raise HTTPException(status_code=500, detail="Failed to send reset email. Please try again later.")
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
# OTP — for guest checkout account creation + password reset
# ─────────────────────────────────────────────────────────────────────────────

_OTP_TTL_MINUTES = 10
_OTP_TABLE = 'auth."OtpStore"'


def _ensure_otp_table(db: Session) -> None:
    # Table is created at startup by OtpStore SQLAlchemy model via create_all().
    pass


def _gen_otp(length: int = 6) -> str:
    return "".join(random.choices(string.digits, k=length))


def _db_otp_store(db: Session, identifier: str, otp: str, purpose: str) -> None:
    """Store or refresh an OTP. identifier may be email or phone number."""
    identifier_type = "email" if "@" in identifier else "phone"
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
    """Fetch the latest OTP row for identifier, optionally filtered by purpose."""
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
    """Delete OTP row(s) for identifier, optionally scoped to a purpose."""
    if purpose:
        db.execute(text(f"""
            DELETE FROM {_OTP_TABLE}
            WHERE "Identifier" = :identifier AND "Purpose" = :purpose
        """), {"identifier": identifier, "purpose": purpose})
    else:
        db.execute(text(f'DELETE FROM {_OTP_TABLE} WHERE "Identifier" = :identifier'),
                   {"identifier": identifier})
    db.commit()


@router.post("/send-otp", summary="Send OTP to email/sms for guest checkout or verification")
async def send_otp(payload: dict, db: Session = Depends(get_db)):
    email   = (payload.get("email") or "").strip().lower()
    phone   = (payload.get("phone") or "").strip()
    purpose = payload.get("purpose", "guest_checkout")

    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Valid email is required.")

    _ensure_otp_table(db)
    otp = _gen_otp()
    _db_otp_store(db, email, otp, purpose)

    # ── Send OTP via email ────────────────────────────────────────────────────
    try:
        from app.sendemail.repository import send_email
        from app.sendemail.schemas import EmailCreate
        send_email(db, EmailCreate(
            name    = email.split("@")[0],
            email   = email,
            subject = "Your OTP — Only TWAM",
            message = (
                f"<p>Your one-time password (OTP) is: <strong style='font-size:24px;letter-spacing:4px;'>{otp}</strong></p>"
                f"<p>Valid for {_OTP_TTL_MINUTES} minutes. Do not share with anyone.</p>"
            ),
            filename=None, isFile=False, data=None,
        ))
    except Exception as e:
        print(f"[Auth] OTP email failed: {e}")

    # ── Send OTP via SMS — SmartPing OTP template: ${var1} = otp ─────────────
    if phone:
        try:
            from app.ekart.notifications import _send_sms
            _send_sms(db, phone, "OTP", [otp])
        except Exception as sms_err:
            print(f"[Auth] OTP SMS failed: {sms_err}")

    return {"success": True, "message": "OTP sent. Please check your email."}


# ─────────────────────────────────────────────────────────────────────────────
# FORGOT PASSWORD — 3-step OTP flow
#   Step 1: POST /auth/forgot-password/check       → what channels does this account have?
#   Step 2: POST /auth/forgot-password/send-otp    → send OTP to chosen channel
#   Step 3: POST /auth/forgot-password/verify-otp  → verify OTP + reset password in one call
# ─────────────────────────────────────────────────────────────────────────────

def _mask_email(email: str) -> str:
    if not email or "@" not in email:
        return email
    local, domain = email.split("@", 1)
    return local[:2] + "***@" + domain


def _mask_phone(phone: str) -> str:
    if not phone or len(phone) < 4:
        return phone
    return phone[:2] + "******" + phone[-2:]


@router.post("/forgot-password/check", summary="Check what channels the account has")
async def forgot_password_check(payload: dict, db: Session = Depends(get_db)):
    identifier = (payload.get("identifier") or "").strip()
    if not identifier:
        raise HTTPException(status_code=400, detail="Identifier is required.")

    user = _get_identity_user_by_username(db, identifier)
    if not user:
        return {"registered": False, "hasEmail": False, "hasPhone": False,
                "maskedEmail": None, "maskedPhone": None}

    has_email = bool(user.Email)
    has_phone = bool(user.PhoneNumber)
    return {
        "registered":  True,
        "hasEmail":    has_email,
        "hasPhone":    has_phone,
        "maskedEmail": _mask_email(user.Email)       if has_email else None,
        "maskedPhone": _mask_phone(user.PhoneNumber) if has_phone else None,
    }


@router.post("/forgot-password/send-otp", summary="Send OTP to chosen channel for password reset")
async def forgot_password_send_otp(payload: dict, db: Session = Depends(get_db)):
    identifier = (payload.get("identifier") or "").strip()
    channel    = (payload.get("channel")    or "email").strip()  # 'email' | 'phone'

    if not identifier:
        raise HTTPException(status_code=400, detail="Identifier is required.")

    user = _get_identity_user_by_username(db, identifier)
    if not user:
        return {"success": True, "message": "If this account exists, an OTP has been sent."}

    _ensure_otp_table(db)
    otp = _gen_otp()
    _db_otp_store(db, identifier, otp, "forgot_password")

    sms_ok = False
    if channel == "phone" and user.PhoneNumber:
        # SmartPing OTP template: ${var1} = otp
        try:
            from app.ekart.notifications import _send_sms
            sms_ok = _send_sms(db, user.PhoneNumber, "OTP", [otp])
            if not sms_ok:
                print(f"[Auth] SMS returned failure for {user.PhoneNumber}")
        except Exception as e:
            print(f"[Auth] forgot-password SMS exception: {e}")
    else:
        try:
            from app.sendemail.repository import send_email
            from app.sendemail.schemas import EmailCreate
            full_name = user.full_name or user.Email or identifier
            send_email(db, EmailCreate(
                name    = full_name,
                email   = user.Email,
                subject = "Reset Your Password — Only TWAM",
                message = (
                    f"<p>Hi {full_name},</p>"
                    f"<p>Your password reset OTP is: "
                    f"<strong style='font-size:24px;letter-spacing:4px;'>{otp}</strong></p>"
                    f"<p>Valid for {_OTP_TTL_MINUTES} minutes. Do not share with anyone.</p>"
                    f"<p>If you did not request this, please ignore this email.</p>"
                ),
                filename=None, isFile=False, data=None,
            ))
        except Exception as e:
            print(f"[Auth] forgot-password email failed: {e}")

    if channel == "phone" and not sms_ok:
        return {"success": True, "smsSent": False,
                "message": "OTP generated but SMS delivery failed. Check SMS credentials/template."}
    return {"success": True, "smsSent": channel == "phone" and sms_ok, "message": "OTP sent."}


@router.post("/forgot-password/verify-otp", summary="Verify OTP and reset password in one call")
async def forgot_password_verify_otp(payload: dict, db: Session = Depends(get_db)):
    identifier   = (payload.get("identifier")   or "").strip()
    otp          = (payload.get("otp")          or "").strip()
    new_password = (payload.get("newPassword")  or "").strip()

    if not identifier or not otp or not new_password:
        raise HTTPException(status_code=400, detail="identifier, otp and newPassword are required.")

    stored = _db_otp_get(db, identifier, purpose="forgot_password")
    if not stored:
        raise HTTPException(status_code=400, detail="OTP not found. Please request a new one.")

    if stored["otp"] != otp:
        raise HTTPException(status_code=400, detail="Invalid OTP. Please check and try again.")

    expires_at = stored["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        _db_otp_delete(db, identifier, purpose="forgot_password")
        raise HTTPException(status_code=400, detail="OTP has expired. Please request a new one.")

    if stored.get("purpose") and stored["purpose"] != "forgot_password":
        raise HTTPException(status_code=400, detail="Invalid OTP purpose.")

    user = _get_identity_user_by_username(db, identifier)
    if not user or not user.Email:
        raise HTTPException(status_code=404, detail="Account not found.")

    result = reset_password_with_token(db, user.Email, new_password)
    if not result["succeeded"]:
        raise HTTPException(status_code=400, detail={"errors": result["errors"]})

    _db_otp_delete(db, identifier, purpose="forgot_password")
    return {"success": True, "message": "Password has been reset successfully. You can now sign in."}


@router.post("/verify-otp", summary="Verify OTP and create guest account if purpose=guest_checkout")
async def verify_otp(payload: dict, db: Session = Depends(get_db)):
    email   = (payload.get("email") or "").strip().lower()
    otp     = (payload.get("otp") or "").strip()
    purpose = payload.get("purpose", "guest_checkout")

    if not email or not otp:
        raise HTTPException(status_code=400, detail="Email and OTP are required.")

    stored = _db_otp_get(db, email, purpose="guest_checkout")
    if not stored:
        raise HTTPException(status_code=400, detail="No OTP found. Please request a new one.")

    if stored["otp"] != otp:
        raise HTTPException(status_code=400, detail="Invalid OTP. Please check and try again.")

    expires_at = stored["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        _db_otp_delete(db, email, purpose="guest_checkout")
        raise HTTPException(status_code=400, detail="OTP has expired. Please request a new one.")

    if purpose == "guest_checkout":
        existing = _get_identity_user_by_username(db, email)
        DEFAULT_PASSWORD = "twam@1234"

        if not existing:
            from app.auth.schemas import RegisterUserRequest as RUR
            first_name = payload.get("firstName") or email.split("@")[0]
            reg = RUR(
                FirstName   = first_name,
                LastName    = payload.get("lastName") or "",
                EmailId     = email,
                Password    = DEFAULT_PASSWORD,
                PhoneNumber = payload.get("phone") or "",
                IsActive    = True,
                RoleId      = Roles.USER,
            )
            result = create_identity_user(db, reg)
            if not result["succeeded"]:
                raise HTTPException(status_code=500, detail="Account creation failed. Please try again.")

        user         = _get_identity_user_by_username(db, email)
        claims       = build_token_claims(db, user)
        access_token = create_access_token(claims)
        _db_otp_delete(db, email, purpose="guest_checkout")

        # ── Send TEMPORARY_PASSWORD SMS to new accounts that provided a phone ─
        # SmartPing TEMPORARY_PASSWORD template:
        #   ${var1} = customer_name  |  ${var2} = password
        phone = (payload.get("phone") or "").strip()
        if not existing and phone:
            try:
                from app.ekart.notifications import _send_sms
                customer_name = payload.get("firstName") or email.split("@")[0]
                _send_sms(db, phone, "TEMPORARY_PASSWORD", [customer_name, DEFAULT_PASSWORD])
            except Exception as sms_err:
                print(f"[Auth] TEMPORARY_PASSWORD SMS failed: {sms_err}")

        return {
            "success":      True,
            "access_token": access_token,
            "token_type":   "Bearer",
            "is_new_user":  not existing,
            "message":      "Account verified successfully.",
        }

    _db_otp_delete(db, email, purpose="guest_checkout")
    return {"success": True, "message": "OTP verified. You may now reset your password."}

_bearer_scheme = _HTTPBearer(auto_error=False)


@router.post("/logout", summary="Logout — invalidate JWT token")
async def logout(
    credentials: _Optional[_Creds] = Depends(_bearer_scheme),
    current_user: CurrentUser      = Depends(get_current_user),
    db: Session                    = Depends(get_db),
):
    """
    Adds the current token to the blacklist table.
    After this, the token returns 401 on every subsequent request.
    The frontend/mobile app should delete the token from local storage after calling this.
    """
    if credentials and credentials.credentials:
        _blacklist_token(
            token=credentials.credentials,
            email=current_user.email,
            db=db,
        )
    return {"success": True, "message": "Logged out successfully."}