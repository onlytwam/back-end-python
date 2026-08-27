"""
app/auth/dependencies.py
─────────────────────────
Checks JWT validity + token blacklist on every protected request.
"""

from typing import Optional
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

import jwt

from app.billing.schemas import TokenPayload

from .security import decode_token, is_token_blacklisted
from .schemas import CurrentUser
from database import SessionLocal
from pydantic import BaseModel as _BaseModel


# ── Role constants ─────────────────────────────────────────────────────────────

class Roles:
    SUPER_ADMIN       = 1
    USER              = 2
    INVENTORY_MANAGER = 3
    PRODUCT_MANAGER   = 4
    ORDER_MANAGER     = 5
    CUSTOMER_SUPPORT  = 6
    WAREHOUSE_MANAGER = 7

    NAMES = {
        1: "Super Admin",
        2: "User",
        3: "Inventory Manager",
        4: "Store Manager",
        5: "Order Manager",
        6: "Customer Support",
        7: "Warehouse Manager",
    }

    STAFF = {1, 3, 4, 5, 6, 7}
    ALL   = {1, 2, 3, 4, 5, 6, 7}


# ── Bearer extractor ───────────────────────────────────────────────────────────

_bearer_scheme = HTTPBearer(auto_error=False)


# ── get_current_user ───────────────────────────────────────────────────────────

async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
)  -> CurrentUser: 
    """
    Validates JWT and checks the token blacklist.
    Returns 401 for missing/expired/blacklisted tokens.
    Returns 403 for invalid tokens.
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please log in.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    # ✅ Check blacklist (logged-out tokens)
    db = SessionLocal()
    try:
        if is_token_blacklisted(token, db):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session ended. Please log in again.",
                headers={"WWW-Authenticate": "Bearer"},
            )
            
        try:
            payload = decode_token(token)
        except jwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Your session has expired. Please log in again.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        except jwt.InvalidTokenError as e:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Invalid authentication token: {str(e)[:100]}",
            )

        email_id = payload.get("email_id") or payload.get("email")
        if not email_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Token missing required claim: email_id",
            )

        from app.auth.models import IdentityUser
        user = db.query(IdentityUser).filter(IdentityUser.Email == email_id).first()
        if not user or user.DeletedInd or not user.IsActive:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Account is deactivated or deleted.",
                headers={"WWW-Authenticate": "Bearer"},
            )
            
        token_stamp = payload.get("SecurityStamp")
        if token_stamp and user.SecurityStamp and token_stamp != user.SecurityStamp:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Credentials changed. Please log in again.",
                headers={"WWW-Authenticate": "Bearer"},
            )

    finally:
        db.close()

    role_id_raw = payload.get("RoleId") or payload.get("roleId") or 0
    try:
        role_id = int(role_id_raw)
    except (TypeError, ValueError):
        role_id = 0

    user_id = str(payload.get("sub") or payload.get("UserId") or "")

    return CurrentUser(
        user_id    = user_id,
        email      = email_id,
        role_id    = role_id,
        role_name  = payload.get("RoleName") or payload.get("role") or "",
        first_name = payload.get("FirstName") or payload.get("given_name") or "",
        last_name         = payload.get("LastName")  or payload.get("family_name") or "",
        full_name         = payload.get("name") or "",
        profile_image_url = payload.get("ProfileImageUrl") or payload.get("profile_image_url") or "",
        tenant_id         = str(payload.get("TenantId") or "1"),
        store_id          = payload.get("StoreId") or payload.get("store_id"),
        warehouse_id=     payload.get("WarehouseId") or payload.get("warehouse_id"),
        warhouse_name=   payload.get("WarehouseName") or payload.get("warehouse_name") or "",
    )


# ── Optional user ──────────────────────────────────────────────────────────────

async def get_optional_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> Optional[CurrentUser]:
    if credentials is None or not credentials.credentials:
        return None
    try:
        return await get_current_user(request, credentials)
    except HTTPException:
        return None


# ── Role-gated factory ─────────────────────────────────────────────────────────

def require_roles(*allowed_role_ids: int):
    allowed = set(allowed_role_ids)

    async def _check(
        current_user: CurrentUser = Depends(get_current_user),
    ) -> CurrentUser:
        if current_user.role_id not in allowed:
            role_names = [Roles.NAMES.get(r, str(r)) for r in sorted(allowed)]
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Required role(s): {', '.join(role_names)}.",
            )
        return current_user

    return _check


# ── Admin user dependency ──────────────────────────────────────────────────────

async def get_current_admin_user(
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    allowed = {Roles.SUPER_ADMIN, Roles.ORDER_MANAGER}
    if current_user.role_id not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin or Order Manager role required.",
        )
    return current_user


# ── Shortcuts ──────────────────────────────────────────────────────────────────

def require_super_admin():
    return require_roles(Roles.SUPER_ADMIN)

def require_staff():
    return require_roles(*Roles.STAFF)

def require_admin_or_order_manager():
    return require_roles(Roles.SUPER_ADMIN, Roles.ORDER_MANAGER)

def require_inventory_access():
    return require_roles(Roles.SUPER_ADMIN, Roles.INVENTORY_MANAGER, Roles.PRODUCT_MANAGER)


# ── require_store_user  (used by billing v2 router) ─────────────
def require_store_user(
    current_user: CurrentUser = Depends(get_current_user),
) -> TokenPayload:                     # ← converts CurrentUser → TokenPayload
    SUPER_ADMIN = 1

    if current_user.role_id == SUPER_ADMIN:
        return TokenPayload(
            sub          = current_user.user_id,
            role_id      = current_user.role_id,
            store_id     = current_user.store_id,
            warehouse_id = current_user.warehouse_id,
        )

    if not current_user.store_id:
        raise HTTPException(
            status_code=403,
            detail="This endpoint requires a store-manager account.",
        )

    return TokenPayload(
        sub          = current_user.user_id,
        role_id      = current_user.role_id,
        store_id     = current_user.store_id,
        warehouse_id = current_user.warehouse_id,
    )



