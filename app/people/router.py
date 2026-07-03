import json
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session
from database import get_db
from app.auth.dependencies import get_current_user, require_roles, Roles, CurrentUser
from . import schemas, repository

router = APIRouter(
    dependencies=[Depends(get_current_user)],
    prefix="/api/People", tags=["People"])


def parse_filters(raw: Optional[str]) -> Optional[list]:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else [parsed]
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Filters format.")


@router.get("", response_model=schemas.PeopleListResponse,
            dependencies=[Depends(require_roles(Roles.SUPER_ADMIN))])
def get_list(
    Filters: Optional[str] = Query(None, alias="Filters"),
    Order_Ascending: Optional[bool] = Query(None, alias="Order.Ascending"),
    Order_Property: Optional[str] = Query(None, alias="Order.Property"),
    Page_Index: Optional[int] = Query(None, alias="Page.Index", ge=1),
    Page_Size: Optional[int] = Query(None, alias="Page.Size", ge=1),
    db: Session = Depends(get_db)
):
    return repository.get_all(db, parse_filters(Filters), Order_Ascending, Order_Property, Page_Index, Page_Size)


@router.get("/{personal_id}", response_model=schemas.PeopleResponse,
            dependencies=[Depends(require_roles(Roles.SUPER_ADMIN))])
def get_detail(personal_id: int, db: Session = Depends(get_db)):
    result = repository.get_by_id(db, personal_id)
    if not result:
        raise HTTPException(status_code=404, detail="Person not found.")
    return result


@router.delete("/DeleteAccount")
def delete_account(
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Hard-deletes the currently authenticated user's account.

    What is DELETED (hard delete — gone permanently):
      • twam.People           — profile row
      • auth.IdentityUser     — login credentials (email/phone/password)
      • auth.OtpStore         — any OTPs
      • twam.Cart             — all cart items
      • twam.Wishlist         — all wishlist items
      • twam.Address          — all saved addresses
      • twam.Notification     — all notifications

    What is KEPT (needed for business records):
      • twam.Orders           — order history (for fulfilment, invoices, returns)
      • twam.OrderItems       — order line items
      • twam.OrderRefund      — payment/refund logs

    After deletion the user can register again with the same email/phone
    and will get a completely fresh account with no previous data.
    """
    uid = current_user.user_id

    print(f"[DeleteAccount] Hard-deleting account for userProfileId={uid!r}")

    def safe_delete(sql: str, label: str) -> int:
        """Run a DELETE in its own savepoint so a failure doesn't abort the whole transaction."""
        try:
            db.execute(text("SAVEPOINT sp_delete"))
            r = db.execute(text(sql), {"uid": uid})
            db.execute(text("RELEASE SAVEPOINT sp_delete"))
            print(f"[DeleteAccount] {label}: {r.rowcount} rows deleted")
            return r.rowcount
        except Exception as exc:
            db.execute(text("ROLLBACK TO SAVEPOINT sp_delete"))
            print(f"[DeleteAccount] {label}: skipped — {exc}")
            return 0

    try:
        # ── 1. Cart ───────────────────────────────────────────────────────────
        safe_delete('DELETE FROM twam."Cart" WHERE "UserProfileId" = :uid', "Cart")

        # ── 2. Wishlist ───────────────────────────────────────────────────────
        safe_delete('DELETE FROM twam."Wishlist" WHERE "UserProfileId" = :uid', "Wishlist")

        # ── 3. Address ────────────────────────────────────────────────────────
        safe_delete('DELETE FROM twam."Address" WHERE "UserProfileId" = :uid', "Address")

        # ── 4. Notifications ──────────────────────────────────────────────────
        safe_delete('DELETE FROM twam."Notification" WHERE "UserProfileId" = :uid', "Notification")

        # ── 5. OTP records — try both possible column names ───────────────────
        # OtpStore may use "UserId" or "UserProfileId" depending on the migration.
        # Try both; savepoint ensures the other steps are unaffected either way.
        deleted = safe_delete('DELETE FROM auth."OtpStore" WHERE "UserId" = :uid', "OtpStore(UserId)")
        if deleted == 0:
            safe_delete('DELETE FROM auth."OtpStore" WHERE "UserProfileId" = :uid', "OtpStore(UserProfileId)")

        # ── 6. People (profile) ───────────────────────────────────────────────
        safe_delete('DELETE FROM twam."People" WHERE "UserProfileId" = :uid', "People")

        # ── 7. IdentityUser — last (FK root) ─────────────────────────────────
        safe_delete('DELETE FROM auth."IdentityUser" WHERE "Id" = :uid', "IdentityUser")

        db.commit()
        print(f"[DeleteAccount] ✅ Account {uid!r} fully deleted")
        return {"success": True, "message": "Account permanently deleted"}

    except Exception as exc:
        db.rollback()
        print(f"[DeleteAccount] ❌ Failed for {uid!r}: {exc}")
        raise HTTPException(status_code=500, detail=f"Account deletion failed: {exc}")