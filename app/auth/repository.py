"""

Auth repository — IMPROVED VERSION using ORM models instead of raw SQL

This replaces the old repository.py that used text() queries everywhere

"""



import base64

import binascii

import logging

import re

import uuid

from datetime import date, datetime, timedelta, timezone

from pathlib import Path

from typing import Optional, Dict, Any

from sqlalchemy.orm import Session

from sqlalchemy import text, or_, func

from fastapi import HTTPException, UploadFile, Request





from .models import IdentityUser
from app.userrole.models import UserRole

from app.people.models import People

from .security import (

    verify_password, generate_sha256_hash_with_salt,

    _get_password_cipher, _aspnet_hash_password, _decrypt_client_password,

)

from .schemas import RegisterUserRequest



# Setup logger

logger = logging.getLogger(__name__)



PEOPLE_SEQ = 'twam."People_PersonalId_seq"'

# Resolve uploads dir relative to this file so it works regardless of cwd

_REPO_ROOT        = Path(__file__).resolve().parent.parent.parent  # project root

# Local fallback paths (used only when AZURE_STORAGE_CONNECTION_STRING is absent)

PROFILE_IMAGE_DIR    = _REPO_ROOT / "uploads" / "profiles"

PROFILE_IMAGE_PREFIX = "/uploads/profiles/"

MAX_PROFILE_IMAGE_BYTES = 5 * 1024 * 1024



# Azure storage helper (imported here; auto-falls-back to local when Azure not configured)

from app.shared.azure_storage import upload_bytes, delete_image as _azure_delete_image, _is_azure_mode
from app.shared.url_helpers import resolve_url, storage_path_for_db

ALLOWED_PROFILE_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

PROFILE_IMAGE_MIME_TO_EXTENSION = {

    "image/jpeg": ".jpg",

    "image/jpg": ".jpg",

    "image/png": ".png",

    "image/webp": ".webp",

}

PROFILE_IMAGE_DATA_URL_PATTERN = re.compile(

    r"^data:(?P<mime>image/[a-zA-Z0-9.+-]+);base64,(?P<data>[A-Za-z0-9+/=\s]+)$",

    re.IGNORECASE | re.DOTALL,

)

PROFILE_IMAGE_FILE_FIELDS = (

    "profileImage",

    "profile_image",

    "image",

    "file",

    "avatar",

)

EMAIL_FIELDS = ("emailId", "EmailId")

FIRST_NAME_FIELDS = ("firstName", "FirstName")

MIDDLE_NAME_FIELDS = ("middleName", "MiddleName")

LAST_NAME_FIELDS = ("lastName", "LastName")

PHONE_FIELDS = ("phoneNumber", "PhoneNumber")

DOB_FIELDS = ("dateOfBirth", "DateOfBirth", "dob")

PROFILE_IMAGE_DATA_FIELDS = (

    "profileImageUrl",

    "ProfileImageUrl",

    "profileImageData",

    "croppedImage",

    "editedImage",

    "imageData",

    "avatarData",

)

REMOVE_PROFILE_IMAGE_FIELDS = (

    "removeProfileImage",

    "RemoveProfileImage",

    "deleteProfileImage",

    "clearProfileImage",

)





# ============================================================================

# HELPER FUNCTIONS - Now using ORM instead of raw SQL

# ============================================================================



def _first_present(container: Any, keys: tuple[str, ...]) -> Any:

    for key in keys:

        if key in container:

            value = container.get(key)

            if value is not None:

                return value

    return None





def _extract_profile_image_file(form: Any) -> Optional[UploadFile]:

    for key in PROFILE_IMAGE_FILE_FIELDS:

        file_obj = form.get(key)

        if getattr(file_obj, "filename", ""):

            return file_obj

    return None





def _coerce_optional_bool(value: Any) -> Optional[bool]:

    if value is None:

        return None

    if isinstance(value, bool):

        return value

    normalized = str(value).strip().lower()

    if normalized in {"1", "true", "yes", "on"}:

        return True

    if normalized in {"0", "false", "no", "off", ""}:

        return False

    return None





def _normalize_profile_update_payload_dict(raw: dict) -> dict:

    normalized = dict(raw)



    if "profileImageUrl" not in normalized:

        fallback_image_data = _first_present(normalized, PROFILE_IMAGE_DATA_FIELDS[1:])

        if fallback_image_data is not None:

            normalized["profileImageUrl"] = fallback_image_data



    if "removeProfileImage" not in normalized:

        remove_flag = _first_present(normalized, REMOVE_PROFILE_IMAGE_FIELDS[1:])

        coerced_remove_flag = _coerce_optional_bool(remove_flag)

        if coerced_remove_flag is not None:

            normalized["removeProfileImage"] = coerced_remove_flag



    return normalized





async def parse_profile_update_payload(

    request: Request,

) -> tuple[RegisterUserRequest, Optional[UploadFile]]:

    content_type = (request.headers.get("content-type") or "").lower()



    def _from_form(form: Any) -> tuple[RegisterUserRequest, Optional[UploadFile]]:

        profile_image_url = _first_present(form, PROFILE_IMAGE_DATA_FIELDS)

        remove_profile_image = _coerce_optional_bool(_first_present(form, REMOVE_PROFILE_IMAGE_FIELDS))

        payload = RegisterUserRequest.model_validate(

            {

                "emailId": _first_present(form, EMAIL_FIELDS),

                "firstName": _first_present(form, FIRST_NAME_FIELDS),

                "middleName": _first_present(form, MIDDLE_NAME_FIELDS),

                "lastName": _first_present(form, LAST_NAME_FIELDS),

                "phoneNumber": _first_present(form, PHONE_FIELDS),

                "dateOfBirth": _first_present(form, DOB_FIELDS),

                "profileImageUrl": profile_image_url,

                "removeProfileImage": remove_profile_image,

            }

        )

        profile_image = _extract_profile_image_file(form)

        return payload, profile_image



    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:

        form = await request.form()

        return _from_form(form)



    try:

        body = await request.json()

    except Exception:

        # Fallback for clients that send FormData but accidentally keep

        # Content-Type as application/json.

        try:

            form = await request.form()

            if form and len(form.keys()) > 0:

                return _from_form(form)

        except Exception:

            pass

        raise HTTPException(status_code=400, detail="Invalid profile update payload.")



    if not isinstance(body, dict):

        raise HTTPException(status_code=400, detail="Invalid profile update payload.")



    return RegisterUserRequest.model_validate(_normalize_profile_update_payload_dict(body)), None



def _get_identity_user_by_username(db: Session, username: str) -> Optional[IdentityUser]:

    """

    Find user by username, email, or phone number.

    Uses ORM query instead of raw SQL.

    """

    identifier = (username or "").strip()

    if not identifier:

        return None



    username_upper = identifier.upper()

    phone_digits = re.sub(r"\D", "", identifier)



    user_conditions = [

        IdentityUser.NormalizedUserName == username_upper,

        IdentityUser.NormalizedEmail == username_upper,

        IdentityUser.Email == identifier,

        IdentityUser.PhoneNumber == identifier,

    ]

    if phone_digits:

        user_conditions.append(

            func.regexp_replace(func.coalesce(IdentityUser.PhoneNumber, ""), r"\D", "", "g") == phone_digits

        )



    user = db.query(IdentityUser).filter(or_(*user_conditions)).first()

    if user:

        return user



    people = db.query(People).filter(People.deletedInd == False)

    if "@" in identifier:

        people = people.filter(func.upper(People.emailId) == username_upper)

    else:

        people_conditions = [People.phoneNumber == identifier]

        if phone_digits:

            people_conditions.append(

                func.regexp_replace(func.coalesce(People.phoneNumber, ""), r"\D", "", "g") == phone_digits

            )

        people = people.filter(or_(*people_conditions))



    person = people.first()

    if not person:

        return None



    if person.userProfileId:

        linked_user = db.query(IdentityUser).filter(IdentityUser.Id == person.userProfileId).first()

        if linked_user:

            return linked_user



    if person.emailId:

        linked_user = db.query(IdentityUser).filter(

            or_(

                IdentityUser.NormalizedEmail == person.emailId.strip().upper(),

                IdentityUser.Email == person.emailId.strip(),

            )

        ).first()

        if linked_user:

            return linked_user



    return None





def _get_role_name(db: Session, role_id: int) -> str:

    """Get role name from UserRole table using ORM"""

    role = db.query(UserRole).filter(

        UserRole.UserRoleId == role_id

    ).first()

    

    return role.RoleName if role else ""



_MAX_FAILED_ATTEMPTS  = 5                  # lock after this many consecutive failures

_LOCKOUT_DURATION_MIN = 30                 # auto-unlock after this many minutes





def _increment_access_failed(db: Session, user: IdentityUser):

    """

    Increment the failed-login counter.

    After _MAX_FAILED_ATTEMPTS failures, set a timed LockoutEnd instead of

    permanently disabling the account — the account auto-unlocks after

    _LOCKOUT_DURATION_MIN minutes without any admin action required.

    """

    user.AccessFailedCount = (user.AccessFailedCount or 0) + 1

    if user.AccessFailedCount >= _MAX_FAILED_ATTEMPTS:

        user.LockoutEnd = datetime.now(timezone.utc) + timedelta(minutes=_LOCKOUT_DURATION_MIN)

        # NOTE: IsActive is NOT set to False — the account unlocks automatically

    db.commit()

    db.refresh(user)





def _reset_access_failed(db: Session, user: IdentityUser):

    """Reset failed access count and clear any lockout on successful login."""

    user.AccessFailedCount = 0

    user.LockoutEnd        = None       # clear timed lockout on successful login

    db.commit()





# ============================================================================

# AUTHENTICATION

# ============================================================================



class AuthResult:

    """Authentication result container"""

    def __init__(self, success: bool, error: str = "", user: IdentityUser = None,

                 lockout_remaining_minutes: int = None, failed_count: int = None):

        self.success = success

        self.error = error

        self.user = user

        self.lockout_remaining_minutes = lockout_remaining_minutes

        self.failed_count = failed_count  # current AccessFailedCount after this attempt





def authenticate_user(db: Session, username: str, encrypted_password: str) -> AuthResult:

    user = _get_identity_user_by_username(db, username)



    if not user:

        return AuthResult(False, "not_allowed")



    if not user.IsActive:

        return AuthResult(False, "deactivated")



    # ── Timed lockout check with auto-unlock ──────────────────────────────────

    if user.LockoutEnd is not None:

        lockout_end = user.LockoutEnd

        if lockout_end.tzinfo is None:

            lockout_end = lockout_end.replace(tzinfo=timezone.utc)

        if datetime.now(timezone.utc) < lockout_end:

            # Still within the lockout window

            remaining = int((lockout_end - datetime.now(timezone.utc)).total_seconds() / 60) + 1

            return AuthResult(False, "locked_out", lockout_remaining_minutes=remaining)

        else:

            # Lockout window has passed — auto-unlock the account

            _reset_access_failed(db, user)



    logger.debug(f"[Auth] stored hash prefix: {user.PasswordHash[:20] if user.PasswordHash else 'NULL'}")

    logger.debug(f"[Auth] AccessFailedCount: {user.AccessFailedCount}")



    is_valid = verify_password(

        encrypted_password,

        user.PasswordHash,

        is_client_encrypted=True

    )



    if not is_valid:

        _increment_access_failed(db, user)



        if user.AccessFailedCount >= _MAX_FAILED_ATTEMPTS:

            return AuthResult(False, "locked_out", failed_count=_MAX_FAILED_ATTEMPTS)



        # Return current count so the frontend can display an accurate attempt counter

        # without relying on a local counter that resets when the user navigates away.

        return AuthResult(False, "invalid_credentials", failed_count=user.AccessFailedCount)



    # Success — reset failed count and clear any leftover lockout

    _reset_access_failed(db, user)

    return AuthResult(True, user=user)





# def build_token_claims(db: Session, user: IdentityUser) -> dict:

#     """

#     Build JWT token claims from user object.

#     IMPROVED: Uses ORM model properties instead of dictionary keys.

#     """

#     role_name = _get_role_name(db, user.UserRoleId or 2)

#     session = str(uuid.uuid4())

    

#     full_name = user.full_name  # Uses the model property

    

    

#     return {

#         "sub": user.Id,

#         "preferred_username": user.UserName,

#         "name": full_name,

#         "sid": session,

#         "email": user.Email or "",

#         "email_id": user.Email or "",

#         "UserId": user.Id,

#         "session_id": session,

#         "TenantId": user.TenantId or "1",

#         "FirstName": user.FirstName or "",

#         "MiddleName": user.MiddleName or "",

#         "LastName": user.LastName or "",

#         "ProfileImageUrl": _normalize_profile_image_url(user.ProfileImageUrl) or "",

#         "RoleId": str(user.UserRoleId or 2),

#         "RoleName": role_name,

#         "role": role_name,

#         "family_name": user.LastName or "",

#         "given_name": user.FirstName or "",

#         "phone_number": user.PhoneNumber or "",

#         "IsRandomPassword": bool(user.IsRandomPassword),

#         "is_random_password": bool(user.IsRandomPassword),

#         "DateOfBirth": user.DateOfBirth.isoformat() if user.DateOfBirth else "",

#         "date_of_birth": user.DateOfBirth.isoformat() if user.DateOfBirth else "",

#         "storeId":       People.StoreId     if People else None,

#         "warehouseId":   People.WarehouseId if People else None,

#         "warehouseName": (

#             People.warehouse.WarehouseName

#             if People and People.warehouse else ""

#         ),

#         "storeName": (

#             People.store.StoreName

#             if People and People.store else ""

#         ),

#     }



def build_token_claims(db: Session, user: IdentityUser) -> dict:

    """

    Build JWT token claims from user object.

    IMPROVED: Uses ORM model properties instead of dictionary keys.

    """

    # Bug 4 fix: do NOT default UserRoleId to 2 — that silently downgrades

    # admin/staff accounts (role 1, 3+) to customer role when UserRoleId is NULL.

    # If UserRoleId is genuinely NULL in the DB, fix the data; don't mask the problem.

    role_id   = user.UserRoleId          # use exact value from DB, no fallback

    role_name = _get_role_name(db, role_id) if role_id is not None else ""

    session   = str(uuid.uuid4())

    full_name = user.full_name



    # ✅ Query the People instance for this user

    person = db.query(People).filter(People.userProfileId == user.Id).first()



    warehouse_name = ""

    store_name     = ""



    if person is not None and person.WarehouseId is not None:

        from app.billing.models import Warehouse

        wh = db.query(Warehouse).filter(Warehouse.WarehouseId == person.WarehouseId).first()

        warehouse_name = str(wh.WarehouseName) if wh and wh.WarehouseName else ""



    if person is not None and person.StoreId is not None:

        from app.stores.models import Stores

        st = db.query(Stores).filter(Stores.storeId == person.StoreId).first()

        store_name = str(st.storeName) if st and st.storeName else ""



    return {

        "sub":                user.Id,

        "preferred_username": user.UserName,

        "name":               full_name,

        "sid":                session,

        "SecurityStamp":      user.SecurityStamp or "",

        "email":              user.Email or "",

        "email_id":           user.Email or "",

        "UserId":             user.Id,

        "session_id":         session,

        "TenantId":           user.TenantId or "1",

        "FirstName":          user.FirstName or "",

        "MiddleName":         user.MiddleName or "",

        "LastName":           user.LastName or "",

        "ProfileImageUrl":    _normalize_profile_image_url(user.ProfileImageUrl) or "",

        "RoleId":             str(role_id) if role_id is not None else "",

        "RoleName":           role_name,

        "role":               role_name,

        "family_name":        user.LastName or "",

        "given_name":         user.FirstName or "",

        "phone_number":       user.PhoneNumber or "",

        "IsRandomPassword":   bool(user.IsRandomPassword),

        "is_random_password": bool(user.IsRandomPassword),

        "DateOfBirth":        user.DateOfBirth.isoformat() if user.DateOfBirth else "",

        "date_of_birth":      user.DateOfBirth.isoformat() if user.DateOfBirth else "",



        # ✅ person = instance, People = class

        "storeId":       int(person.StoreId)     if person is not None and person.StoreId     is not None else None,

        "warehouseId":   int(person.WarehouseId) if person is not None and person.WarehouseId is not None else None,

        "warehouseName": warehouse_name,

        "storeName":     store_name,

    }

# ============================================================================

# USER REGISTRATION

# ============================================================================



def _parse_dob(value: Any) -> Optional[date]:

    """Accept a date object, ISO string (YYYY-MM-DD), or None. Returns date or None."""

    if value is None:

        return None

    if isinstance(value, date):

        return value

    try:

        return date.fromisoformat(str(value).strip())

    except (ValueError, TypeError):

        return None





def _next_people_id(db: Session) -> Optional[int]:

    """Get next People ID from sequence"""

    try:

        row = db.execute(text(f"SELECT nextval('{PEOPLE_SEQ}')")).fetchone()

        return int(row[0])

    except Exception as e:

        logger.error(f"Sequence error: {e}", exc_info=True)

        return None





def _normalize_profile_image_url(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    return resolve_url(str(path).strip().replace("\\", "/"))


def _profile_image_path_for_db(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    return storage_path_for_db(str(path).strip().replace("\\", "/"))





def _delete_profile_image_file(path: Optional[str]) -> None:

    """

    Delete a profile image from Azure Blob (prod) or local disk (dev).

    The path may be a full Azure URL or a local /uploads/… path.

    """

    normalized = _normalize_profile_image_url(path)

    if not normalized:

        return



    # Delegate to the unified azure_storage helper — it handles both modes

    _azure_delete_image(normalized)





def _detect_profile_image_extension(image_bytes: bytes) -> Optional[str]:

    if image_bytes.startswith(b"\xff\xd8\xff"):

        return ".jpg"

    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):

        return ".png"

    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":

        return ".webp"

    return None





def _is_likely_base64_profile_image_payload(raw_payload: str) -> bool:

    payload = (raw_payload or "").strip()

    if not payload:

        return False



    if PROFILE_IMAGE_DATA_URL_PATTERN.match(payload):

        return True



    if payload.startswith("http://") or payload.startswith("https://") or payload.startswith("/"):

        return False



    compact_payload = payload.replace("\n", "").replace("\r", "").strip()

    if len(compact_payload) < 128:

        return False



    return bool(re.fullmatch(r"[A-Za-z0-9+/=]+", compact_payload))





def _decode_base64_profile_image_payload(image_payload: str) -> tuple[bytes, str]:

    raw_payload = (image_payload or "").strip()

    if not raw_payload:

        raise HTTPException(status_code=400, detail="Profile image data is empty.")



    mime_extension: Optional[str] = None

    base64_data = raw_payload



    data_url_match = PROFILE_IMAGE_DATA_URL_PATTERN.match(raw_payload)

    if data_url_match:

        mime_type = data_url_match.group("mime").lower()

        mime_extension = PROFILE_IMAGE_MIME_TO_EXTENSION.get(mime_type)

        base64_data = data_url_match.group("data")



    try:

        image_bytes = base64.b64decode(base64_data, validate=True)

    except (binascii.Error, ValueError) as exc:

        raise HTTPException(status_code=400, detail="Invalid base64 image payload.") from exc



    if not image_bytes:

        raise HTTPException(status_code=400, detail="Profile image data is empty.")



    if len(image_bytes) > MAX_PROFILE_IMAGE_BYTES:

        raise HTTPException(status_code=400, detail="Profile image must be 5MB or smaller.")



    detected_extension = _detect_profile_image_extension(image_bytes)

    extension = detected_extension or mime_extension



    if not extension or extension not in ALLOWED_PROFILE_IMAGE_EXTENSIONS:

        raise HTTPException(

            status_code=400,

            detail="Profile image must be JPG, JPEG, PNG, or WEBP.",

        )



    return image_bytes, extension





def _save_profile_image_file(upload: UploadFile, user_id: str) -> str:

    """

    Save an uploaded profile image.

    On Azure (prod): uploads to the 'profiles' container and returns the full blob URL.

    Locally (dev):   saves to uploads/profiles/ and returns a /uploads/profiles/… path.

    """

    original_name = (upload.filename or "").strip()

    extension = Path(original_name).suffix.lower()

    if extension not in ALLOWED_PROFILE_IMAGE_EXTENSIONS:

        raise HTTPException(

            status_code=400,

            detail="Profile image must be JPG, JPEG, PNG, or WEBP.",

        )



    content_type = (upload.content_type or "").lower()

    if content_type and not content_type.startswith("image/"):

        raise HTTPException(status_code=400, detail="Invalid profile image file type.")



    # Read file bytes (enforcing size limit)

    chunks: list[bytes] = []

    total_size = 0

    try:

        while True:

            chunk = upload.file.read(1024 * 1024)

            if not chunk:

                break

            total_size += len(chunk)

            if total_size > MAX_PROFILE_IMAGE_BYTES:

                raise HTTPException(status_code=400, detail="Profile image must be 5MB or smaller.")

            chunks.append(chunk)

    finally:

        try:

            upload.file.close()

        except Exception:

            pass



    file_bytes = b"".join(chunks)

    if not file_bytes:

        raise HTTPException(status_code=400, detail="Profile image is empty.")



    safe_user_id = "".join(ch for ch in user_id if ch.isalnum() or ch in {"-", "_"}) or uuid.uuid4().hex

    filename = f"{safe_user_id}_{uuid.uuid4().hex[:8]}{extension}"



    return upload_bytes(

        file_bytes=file_bytes,

        filename=filename,

        content_type=content_type or f"image/{extension.lstrip('.')}",

        container_key="profiles",

        folder=safe_user_id,      # each user gets their own sub-folder

    )





def _save_profile_image_data(image_payload: str, user_id: str) -> str:

    """

    Save a base-64-encoded profile image (e.g. from a cropped canvas export).

    Returns the Azure URL or local path.

    """

    image_bytes, extension = _decode_base64_profile_image_payload(image_payload)



    safe_user_id = "".join(ch for ch in user_id if ch.isalnum() or ch in {"-", "_"}) or uuid.uuid4().hex

    filename     = f"{safe_user_id}_{uuid.uuid4().hex[:8]}{extension}"

    content_type = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",

                    ".png": "image/png",  ".webp": "image/webp"}.get(extension, "image/jpeg")



    return upload_bytes(

        file_bytes=image_bytes,

        filename=filename,

        content_type=content_type,

        container_key="profiles",

        folder=safe_user_id,      # each user gets their own sub-folder

    )





def update_identity_user_profile(

    db: Session,

    user_id: str,

    request: RegisterUserRequest,

    profile_image: Optional[UploadFile] = None,

) -> dict:

    if not user_id:

        raise HTTPException(status_code=401, detail="Authentication required.")



    user = get_user_by_id(db, user_id)

    if not user:

        raise HTTPException(status_code=404, detail="User not found.")



    next_email = (request.EmailId or user.Email or "").strip().lower()

    next_first_name = (request.FirstName or "").strip()

    next_middle_name = (request.MiddleName or "").strip()

    next_last_name = (request.LastName or "").strip()

    next_phone = (request.PhoneNumber or "").strip()

    next_dob = _parse_dob(request.DateOfBirth) if request.DateOfBirth is not None else user.DateOfBirth



    if not next_email:

        raise HTTPException(status_code=400, detail="EmailId is required.")



    if next_email.upper() != (user.NormalizedEmail or ""):

        existing_email_user = db.query(IdentityUser).filter(

            IdentityUser.NormalizedEmail == next_email.upper(),

            IdentityUser.Id != user.Id,

        ).first()

        if existing_email_user:

            raise HTTPException(status_code=400, detail="Email is already taken.")



    if next_phone and next_phone != (user.PhoneNumber or ""):

        existing_phone_user = db.query(IdentityUser).filter(

            IdentityUser.PhoneNumber == next_phone,

            IdentityUser.Id != user.Id,

        ).first()

        if existing_phone_user:

            raise HTTPException(status_code=400, detail="Phone number is already taken.")



    existing_image = _profile_image_path_for_db(user.ProfileImageUrl)

    incoming_profile_image_value = request.ProfileImageUrl

    has_profile_image_value = incoming_profile_image_value is not None

    incoming_profile_image_value = (

        str(incoming_profile_image_value).strip() if incoming_profile_image_value is not None else None

    )

    remove_profile_image = bool(request.RemoveProfileImage)

    incoming_profile_image_is_edited_payload = bool(

        incoming_profile_image_value and _is_likely_base64_profile_image_payload(incoming_profile_image_value)

    )



    has_new_image = bool(profile_image and getattr(profile_image, "filename", ""))

    if has_new_image and remove_profile_image:

        raise HTTPException(

            status_code=400,

            detail="Cannot upload a profile image and remove it in the same request.",

        )



    image_will_change = has_new_image or remove_profile_image

    if has_profile_image_value:

        if not incoming_profile_image_value:

            image_will_change = True

        elif incoming_profile_image_is_edited_payload:

            image_will_change = True

        else:

            requested_profile_image = _profile_image_path_for_db(incoming_profile_image_value)

            image_will_change = image_will_change or requested_profile_image != existing_image



    no_change = (

        next_email.upper() == (user.NormalizedEmail or "") and

        next_first_name == (user.FirstName or "") and

        next_middle_name == (user.MiddleName or "") and

        next_last_name == (user.LastName or "") and

        next_phone == (user.PhoneNumber or "") and

        next_dob == user.DateOfBirth and

        not image_will_change

    )

    if no_change:

        raise HTTPException(status_code=400, detail="No changes detected. Profile is already up to date.")



    saved_image_path: Optional[str] = None

    saved_original_image_path: Optional[str] = None

    original_profile_image_path: Optional[str] = None

    next_profile_image = existing_image

    preserve_existing_image = False



    try:

        if has_new_image and profile_image is not None:

            # Keep the original uploaded image.

            saved_original_image_path = _save_profile_image_file(profile_image, user.Id)

            original_profile_image_path = saved_original_image_path



            # If client also sends cropped/edited output, use it as display image.

            if incoming_profile_image_is_edited_payload and incoming_profile_image_value:

                saved_image_path = _save_profile_image_data(incoming_profile_image_value, user.Id)

                next_profile_image = saved_image_path

            else:

                next_profile_image = saved_original_image_path

        elif remove_profile_image:

            next_profile_image = None

        elif has_profile_image_value:

            if not incoming_profile_image_value:

                next_profile_image = None

            elif incoming_profile_image_is_edited_payload:

                saved_image_path = _save_profile_image_data(incoming_profile_image_value, user.Id)

                next_profile_image = saved_image_path

                original_profile_image_path = existing_image

                preserve_existing_image = bool(existing_image)

            else:

                next_profile_image = _profile_image_path_for_db(incoming_profile_image_value)



        user.UserName = next_email

        user.NormalizedUserName = next_email.upper()

        user.Email = next_email

        user.NormalizedEmail = next_email.upper()

        user.FirstName = next_first_name

        user.MiddleName = next_middle_name

        user.LastName = next_last_name

        user.PhoneNumber = next_phone

        user.DateOfBirth = next_dob

        user.ProfileImageUrl = next_profile_image

        user.ModifiedDate = datetime.now(timezone.utc)

        user.ModifiedBy = user_id



        db.flush()



        people_profile = db.query(People).filter(People.userProfileId == user.Id).first()

        if people_profile is None:

            people_profile = People(

                personalId=_next_people_id(db),

                firstName=next_first_name,

                middleName=next_middle_name,

                lastName=next_last_name,

                dateOfBirth=next_dob,

                emailId=next_email,

                phoneNumber=next_phone,

                profileImageUrl=next_profile_image,

                userProfileId=user.Id,

                isActive=user.IsActive,

                roleId=user.UserRoleId,

                createdDate=datetime.now(timezone.utc),

                modifiedDate=datetime.now(timezone.utc),

                deletedInd=False,

                createdBy=user.Email,

                modifiedBy=user_id,

                organizationId=user.OrganizationId,

            )

            db.add(people_profile)

        else:

            people_profile.firstName = next_first_name

            people_profile.middleName = next_middle_name

            people_profile.lastName = next_last_name

            people_profile.dateOfBirth = next_dob

            people_profile.emailId = next_email

            people_profile.phoneNumber = next_phone

            people_profile.profileImageUrl = next_profile_image

            people_profile.isActive = user.IsActive

            people_profile.roleId = user.UserRoleId

            people_profile.modifiedDate = datetime.now(timezone.utc)

            people_profile.modifiedBy = user_id



        db.commit()

    except HTTPException:

        db.rollback()

        if saved_image_path:

            _delete_profile_image_file(saved_image_path)

        if saved_original_image_path and saved_original_image_path != saved_image_path:

            _delete_profile_image_file(saved_original_image_path)

        raise

    except Exception as exc:

        db.rollback()

        if saved_image_path:

            _delete_profile_image_file(saved_image_path)

        if saved_original_image_path and saved_original_image_path != saved_image_path:

            _delete_profile_image_file(saved_original_image_path)

        logger.error("Failed to update user profile: %s", exc, exc_info=True)

        raise HTTPException(status_code=500, detail="Failed to update profile.") from exc



    if existing_image and existing_image != next_profile_image and not preserve_existing_image:

        _delete_profile_image_file(existing_image)



    response = {

        "message": "User updated successfully.",

        "userId": user.Id,

        "emailId": next_email,

        "firstName": next_first_name,

        "middleName": next_middle_name,

        "lastName": next_last_name,

        "phoneNumber": next_phone,

        "dateOfBirth": next_dob.isoformat() if next_dob else None,

        "profileImageUrl": _normalize_profile_image_url(next_profile_image),

    }

    if original_profile_image_path:

        response["profileImageOriginalUrl"] = _normalize_profile_image_url(original_profile_image_path)



    return response



def create_identity_user(db: Session, request: RegisterUserRequest) -> dict:

    try:

        primary_identifier = (request.EmailId or request.PhoneNumber or "").strip()

        if not primary_identifier:

            return {

                "succeeded": False,

                "errors": [{"code": "InvalidUser", "description": "Email or phone number is required."}]

            }



        # Check for duplicate phone number

        if request.PhoneNumber:

            existing = _get_identity_user_by_username(db, request.PhoneNumber)

            if existing:

                return {

                    "succeeded": False,

                    "errors": [{"code": "DuplicatePhoneNumber", "description": "Phone number is already taken."}]

                }

        

        # Check for duplicate email

        if request.EmailId:

            existing = _get_identity_user_by_username(db, request.EmailId)

            if existing:

                return {

                    "succeeded": False,

                    "errors": [{"code": "DuplicateEmail", "description": "Email is already taken."}]

                }

        

        # ── FIX: Decrypt before hashing (frontend sends encrypted password) ──

        raw_password = request.Password or "Admin@12345"

        try:

            plain_password = _decrypt_client_password(raw_password)

        except Exception:

            plain_password = raw_password  # fallback: treat as plain text

        

        twam_hash   = generate_sha256_hash_with_salt(plain_password, _get_password_cipher())

        stored_hash = _aspnet_hash_password(twam_hash)

        

        # Create new user using ORM

        new_user = IdentityUser(

            Id=str(uuid.uuid4()),

            UserName=primary_identifier,

            NormalizedUserName=primary_identifier.upper(),

            Email=request.EmailId or None,

            NormalizedEmail=(request.EmailId or "").upper() if request.EmailId else None,

            EmailConfirmed=bool(request.EmailId),

            PasswordHash=stored_hash,

            SecurityStamp=str(uuid.uuid4()),

            ConcurrencyStamp=str(uuid.uuid4()),

            PhoneNumber=request.PhoneNumber or None,

            PhoneNumberConfirmed=False,

            TwoFactorEnabled=False,

            LockoutEnabled=True,

            AccessFailedCount=0,

            FirstName=request.FirstName or "",

            MiddleName=request.MiddleName or "",

            LastName=request.LastName or "",

            DateOfBirth=_parse_dob(request.DateOfBirth),

            ProfileImageUrl=_profile_image_path_for_db(request.ProfileImageUrl),

            IsActive=True,

            UserRoleId=request.RoleId if request.RoleId else 2,

            CreatedDate=datetime.now(timezone.utc),

            CreatedBy=request.EmailId or request.PhoneNumber or "system",

            TenantId="1",

            OrganizationId=1,

            DeletedInd=False,

            IsRandomPassword=bool(request.TemporaryPassword)

        )

        

        db.add(new_user)

        db.flush()  # Get the ID without committing

        

        # Insert into twam.People

        people_id = _next_people_id(db)

        if people_id:

            db.add(

                People(

                    personalId=people_id,

                    firstName=request.FirstName or "",

                    middleName=request.MiddleName or "",

                    lastName=request.LastName or "",

                    dateOfBirth=_parse_dob(request.DateOfBirth),

                    emailId=request.EmailId or None,

                    phoneNumber=request.PhoneNumber or None,

                    profileImageUrl=_profile_image_path_for_db(request.ProfileImageUrl),

                    userProfileId=new_user.Id,

                    isActive=True,

                    roleId=new_user.UserRoleId,

                    createdDate=datetime.now(timezone.utc),

                    deletedInd=False,

                    createdBy=request.EmailId or request.PhoneNumber or "system",

                    organizationId=1,

                )

            )



        db.commit()



        # ── Welcome notifications (fire-and-forget, never block registration) ──

        _send_welcome_notifications(

            db               = db,

            email            = request.EmailId,

            phone            = request.PhoneNumber,

            first_name       = request.FirstName or "",

            temporary_password = request.TemporaryPassword,

        )



        return {

            "succeeded": True,

            "errors": [],

            "user_id": new_user.Id

        }

        

    except Exception as e:

        db.rollback()

        logger.error(f"Failed to create user: {str(e)}", exc_info=True)

        raise HTTPException(

            status_code=500,

            detail=f"Failed to create user: {str(e)}"

        )





def _send_welcome_notifications(

    db:         "Session",

    email:      Optional[str],

    phone:      Optional[str],

    first_name: str = "",

    temporary_password: Optional[str] = None,

) -> None:

    """

    Send welcome email and/or WhatsApp to a newly registered user.

    Rules:

      - Email given  → send welcome email

      - Phone given  → send welcome WhatsApp

      - Both given   → send both

      - Neither      → skip silently

    Never raises — registration must not fail because of notification errors.

    """

    from app.ekart.notifications import MT_WELCOME, _send_sms

    from app.whatsapp.service import send_whatsapp

    from app.sendemail.repository import send_email

    from app.sendemail.schemas import EmailCreate

    from html import escape



    name = first_name.strip() or "Valued Customer"

    temp_password_block = ""

    if temporary_password:

        safe_temp_password = escape(temporary_password)

        temp_password_block = f"""

                <div style="margin:28px 0;padding:18px;border:1px dashed #6B0F2A;border-radius:12px;background:#fff8f9;">

                  <p style="margin:0 0 8px;color:#6B0F2A;font-weight:700;">Your temporary password</p>

                  <div style="font-size:22px;letter-spacing:3px;font-weight:700;color:#222;">{safe_temp_password}</div>

                  <p style="margin:10px 0 0;color:#444;line-height:1.7;">

                    Please sign in and change this password after your first login.

                  </p>

                </div>

        """



    # ── Welcome Email ─────────────────────────────────────────────────────────

    if email and "@" in email:

        try:

            body = f"""

            <div style="max-width:600px;margin:0 auto;font-family:'Trebuchet MS',Arial,sans-serif;">

              <div style="background:#6B0F2A;padding:24px 32px;text-align:center;">

                <h2 style="color:#F5F0E6;margin:0;font-size:24px;letter-spacing:2px;">TWAM</h2>

              </div>

              <div style="padding:32px 24px;">

                <h2 style="color:#6B0F2A;">Welcome, {name}! 🎉</h2>

                <p style="color:#444;line-height:1.7;">

                  We're thrilled to have you at <strong>Only TWAM</strong> — your destination

                  for premium and elegant lingerie crafted with love.

                </p>

                <p style="color:#444;line-height:1.7;">

                  Explore styles that celebrate you and discover comfort that fits perfectly.

                </p>

                {temp_password_block}

                <div style="text-align:center;margin:28px 0;">

                  <a href="https://www.onlytwam.com/all-products"

                     style="background:#6B0F2A;color:#fff;padding:13px 32px;

                            border-radius:8px;text-decoration:none;font-weight:600;font-size:15px;">

                    Start Shopping →

                  </a>

                </div>

                <p style="color:#888;font-size:13px;text-align:center;">

                  Questions? Reach us at

                  <a href="mailto:support@onlytwam.com" style="color:#6B0F2A;">support@onlytwam.com</a>

                </p>

              </div>

              <div style="background:#f9f3f5;padding:16px;text-align:center;font-size:12px;color:#aaa;">

                © 2025 TWAM · GSM Mall, Miyapur Main Rd, Hyderabad 500050

              </div>

            </div>"""

            send_email(db, EmailCreate(

                name    = name,

                email   = email,

                subject = f"Welcome to TWAM, {name}! 🎉",

                message = body,

            ))

            print(f"[Welcome] Email sent to {email}")

        except Exception as e:

            print(f"[Welcome] Email failed for {email}: {e}")



    # ── Welcome WhatsApp ──────────────────────────────────────────────────────

    if phone and phone.strip():

        try:

            wa_msg = (

                "Hi " + name + "! \n\n"

                "Welcome to *TWAM* - your destination for premium & elegant lingerie!\n\n"

                "Explore styles that celebrate you:\n"

                "https://www.onlytwam.com/all-products\n\n"

                "Need help? Email support@onlytwam.com\n\n"

                "- TWAM Team"

            )

            send_whatsapp(db, phone, wa_msg, "WELCOME")

            print(f"[Welcome] WhatsApp sent to {phone}")

        except Exception as e:

            print(f"[Welcome] WhatsApp failed for {phone}: {e}")





# ============================================================================

# PASSWORD MANAGEMENT

# ============================================================================



def reset_password_with_token(db: Session, email: str, new_password: str) -> dict:

    """

    Reset password after token validation.

    IMPROVED: Uses ORM model instead of raw UPDATE.

    """

    try:

        user = _get_identity_user_by_username(db, email)

        

        if not user:

            return {

                "succeeded": False,

                "errors": [{

                    "code": "NotFound",

                    "description": "User not found."

                }]

            }

        

        # Decrypt password

        try:

            new_plain = _decrypt_client_password(new_password)

        except Exception:

            # If decryption fails, treat as plain text

            new_plain = new_password

        

        # Hash the new password

        new_twam = generate_sha256_hash_with_salt(new_plain, _get_password_cipher())

        new_db_hash = _aspnet_hash_password(new_twam)

        

        # Update user using ORM

        user.PasswordHash = new_db_hash

        user.SecurityStamp = str(uuid.uuid4())

        user.ModifiedDate = datetime.now(timezone.utc)

        user.ModifiedBy = email

        user.AccessFailedCount = 0

        user.LockoutEnd        = None   # clear timed lockout — password reset always unlocks

        user.IsRandomPassword  = False

        # NOTE: IsActive is intentionally NOT touched here.

        # Lockout is handled purely via LockoutEnd — IsActive is only

        # changed by admins explicitly (updateUser / deleteUser endpoints).

        

        db.commit()

        return {"succeeded": True, "errors": []}

        

    except Exception as e:

        db.rollback()

        logger.error(f"Failed to reset password: {str(e)}", exc_info=True)

        raise HTTPException(

            status_code=500,

            detail=f"Failed to reset password: {str(e)}"

        )





def change_password(db: Session, email_id: str, current_password: str, new_password: str) -> dict:

    """

    Change password with current password verification.

    IMPROVED: Uses ORM model instead of raw UPDATE.

    """

    try:

        user = _get_identity_user_by_username(db, email_id)

        

        if not user:

            return {

                "succeeded": False,

                "errors": [{

                    "code": "NotFound",

                    "description": "User not found."

                }]

            }

        

        # Verify current password

        if not verify_password(current_password, user.PasswordHash, is_client_encrypted=True):

            return {

                "succeeded": False,

                "errors": [{

                    "code": "PasswordMismatch",

                    "description": "Current password is incorrect."

                }]

            }

        

        # Decrypt and hash new password

        new_plain = _decrypt_client_password(new_password)

        new_twam = generate_sha256_hash_with_salt(new_plain, _get_password_cipher())

        new_db_hash = _aspnet_hash_password(new_twam)

        

        # Update using ORM

        user.PasswordHash = new_db_hash

        user.SecurityStamp = str(uuid.uuid4())

        user.ModifiedDate = datetime.now(timezone.utc)

        user.ModifiedBy = email_id

        user.IsRandomPassword = False



        db.commit()

        return {"succeeded": True, "errors": []}

        

    except Exception as e:

        db.rollback()

        logger.error(f"Failed to change password: {str(e)}", exc_info=True)

        raise HTTPException(

            status_code=500,

            detail=f"Failed to change password: {str(e)}"

        )





def change_password_for_authenticated_user(db: Session, email_id: str, new_password: str) -> dict:

    """

    Change password for an authenticated user.

    The caller is already authenticated via the session/JWT, so this path

    only updates the password hash for the current account.

    """

    try:

        user = _get_identity_user_by_username(db, email_id)



        if not user:

            return {

                "succeeded": False,

                "errors": [{

                    "code": "NotFound",

                    "description": "User not found."

                }]

            }



        # Decrypt and hash new password

        new_plain = _decrypt_client_password(new_password)

        new_twam = generate_sha256_hash_with_salt(new_plain, _get_password_cipher())

        new_db_hash = _aspnet_hash_password(new_twam)



        # Update using ORM

        user.PasswordHash = new_db_hash

        user.SecurityStamp = str(uuid.uuid4())

        user.ModifiedDate = datetime.now(timezone.utc)

        user.ModifiedBy = email_id

        user.IsRandomPassword = False



        db.commit()

        return {"succeeded": True, "errors": []}



    except Exception as e:

        db.rollback()

        logger.error(f"Failed to change password for authenticated user: {str(e)}", exc_info=True)

        raise HTTPException(

            status_code=500,

            detail=f"Failed to change password: {str(e)}"

        )





# ============================================================================

# ADDITIONAL HELPFUL FUNCTIONS

# ============================================================================



def get_user_by_id(db: Session, user_id: str) -> Optional[IdentityUser]:

    """Get user by ID using ORM"""

    return db.query(IdentityUser).filter(

        IdentityUser.Id == user_id,

        IdentityUser.DeletedInd == False

    ).first()





def get_user_by_email(db: Session, email: str) -> Optional[IdentityUser]:

    """Get user by email using ORM"""

    return db.query(IdentityUser).filter(

        IdentityUser.NormalizedEmail == email.upper(),

        IdentityUser.DeletedInd == False

    ).first()





def deactivate_user(db: Session, user_id: str) -> bool:

    """Soft delete/deactivate user"""

    try:

        user = get_user_by_id(db, user_id)

        if not user:

            return False

        

        user.IsActive = False

        user.DeletedInd = True

        user.ModifiedDate = datetime.now(timezone.utc)

        

        db.commit()

        return True

        

    except Exception as e:

        db.rollback()

        logger.error(f"Failed to deactivate user: {str(e)}", exc_info=True)

        return False
