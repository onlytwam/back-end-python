from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import or_
from app.shared.filters import apply_filters, apply_ordering
from typing import Optional, List
from datetime import datetime, timezone

from .models import Coupons, CouponUsage, PhysicalCoupon


# ─────────────────────────────────────────────
# INTERNAL HELPER: used coupon ids for a user
# ─────────────────────────────────────────────
def _used_coupon_ids(db: Session, user_profile_id: str) -> set:
    """Return the set of couponIds already redeemed by this user."""
    rows = (
        db.query(CouponUsage.couponId)
        .filter(CouponUsage.userProfileId == user_profile_id)
        .all()
    )
    return {r.couponId for r in rows}


# ─────────────────────────────────────────────
# GET ALL COUPONS (admin)
# ─────────────────────────────────────────────
def get_all_coupons(
    db: Session,
    filters=None,
    order_ascending=None,
    order_property=None,
    page_index=None,
    page_size=None,
):
    query = db.query(Coupons).filter(Coupons.deletedInd == False)

    if filters:
        code_filters  = [f for f in filters if f.get("property", "").lower() == "couponcode"]
        other_filters = [f for f in filters if f.get("property", "").lower() != "couponcode"]
        for cf in code_filters:
            query = query.filter(Coupons.couponCode.ilike(cf.get("value", "")))
        filters = other_filters if other_filters else None

    query = apply_filters(query, Coupons, filters)
    query = apply_ordering(query, Coupons, order_property, order_ascending)

    total = query.count()

    if page_index and page_size:
        offset = (page_index - 1) * page_size
        query  = query.offset(offset).limit(page_size)

    return {"list": query.all(), "count": total}


# ─────────────────────────────────────────────
# GET USER COUPONS (profile + cart dropdown)
# Only digital coupons; excludes already-used ones.
# ─────────────────────────────────────────────
def get_user_coupons(
    db: Session,
    user_profile_id: Optional[str] = None,
    filters=None,
    order_ascending=None,
    order_property=None,
    page_index=None,
    page_size=None,
):
    now = datetime.now()

    query = db.query(Coupons).filter(
        Coupons.deletedInd == False,
        Coupons.isActive   == True,
    )

    # Scope: generic (isCommon) or assigned to this user.
    # Product-specific coupons ARE included; the frontend filters by cart contents.
    if user_profile_id:
        query = query.filter(
            or_(
                Coupons.isCommon        == True,
                Coupons.toUserProfileId == user_profile_id,
            )
        )
    else:
        query = query.filter(Coupons.isCommon == True)

    # Exclude expired
    query = query.filter(
        or_(Coupons.endDate == None, Coupons.endDate >= now)
    )

    # EXCLUDE coupons already used by this user
    if user_profile_id:
        used_ids = _used_coupon_ids(db, user_profile_id)
        if used_ids:
            query = query.filter(Coupons.couponId.notin_(used_ids))

    query = apply_filters(query, Coupons, filters)
    query = apply_ordering(query, Coupons, order_property, order_ascending)

    total   = query.count()

    if page_index and page_size:
        offset = (page_index - 1) * page_size
        query  = query.offset(offset).limit(page_size)

    results = query.all()
    for coupon in results:
        coupon.isExpire = False

    return {"list": results, "count": total}


# ─────────────────────────────────────────────
# GET COUPON BY ID
# ─────────────────────────────────────────────
def get_coupon_by_id(db: Session, coupon_id: int):
    return db.query(Coupons).filter(
        Coupons.couponId  == coupon_id,
        Coupons.deletedInd == False,
    ).first()


# ─────────────────────────────────────────────
# CREATE COUPON
# ─────────────────────────────────────────────
def create_coupon(db: Session, coupon_data: dict):
    if isinstance(coupon_data.get("startDate"), str):
        coupon_data["startDate"] = datetime.fromisoformat(coupon_data["startDate"])
    if isinstance(coupon_data.get("endDate"), str):
        coupon_data["endDate"] = datetime.fromisoformat(coupon_data["endDate"])

    now = datetime.now()
    if coupon_data.get("startDate") and coupon_data["startDate"].date() < now.date():
        raise HTTPException(400, "Start date cannot be in the past")
    if coupon_data.get("endDate") and coupon_data["endDate"].date() < now.date():
        raise HTTPException(400, "End date cannot be in the past")
    if coupon_data.get("startDate") and coupon_data.get("endDate"):
        if coupon_data["endDate"] < coupon_data["startDate"]:
            raise HTTPException(400, "End date must be after start date")

    # Auto-derive isCommon from couponType when not set explicitly
    if "isCommon" not in coupon_data or coupon_data.get("isCommon") is None:
        coupon_data["isCommon"] = coupon_data.get("couponType") == "generic"

    coupon = Coupons(
        **coupon_data,
        createdDate=datetime.now(),
        deletedInd=False,
    )
    db.add(coupon)
    db.commit()
    db.refresh(coupon)
    return coupon


# ─────────────────────────────────────────────
# UPDATE COUPON
# ─────────────────────────────────────────────
def update_coupon(db: Session, coupon_id: int, coupon_data: dict):
    coupon = get_coupon_by_id(db, coupon_id)
    if not coupon:
        return None

    now = datetime.now(timezone.utc)

    for field in ("startDate", "endDate"):
        if isinstance(coupon_data.get(field), str):
            coupon_data[field] = datetime.fromisoformat(coupon_data[field])
        if coupon_data.get(field) and coupon_data[field].tzinfo is None:
            coupon_data[field] = coupon_data[field].replace(tzinfo=timezone.utc)

    if "startDate" in coupon_data and coupon_data["startDate"]:
        if coupon_data["startDate"].date() < now.date():
            raise HTTPException(400, "Start date cannot be in the past")
    if "endDate" in coupon_data and coupon_data["endDate"]:
        if coupon_data["endDate"].date() < now.date():
            raise HTTPException(400, "End date cannot be in the past")
    if coupon_data.get("startDate") and coupon_data.get("endDate"):
        if coupon_data["endDate"] < coupon_data["startDate"]:
            raise HTTPException(400, "End date must be after start date")

    for key, value in coupon_data.items():
        setattr(coupon, key, value)
    coupon.modifiedDate = now
    db.commit()
    db.refresh(coupon)
    return coupon


# ─────────────────────────────────────────────
# DELETE COUPON (SOFT DELETE)
# ─────────────────────────────────────────────
def delete_coupon(db: Session, coupon_id: int):
    coupon = get_coupon_by_id(db, coupon_id)
    if not coupon:
        return None
    coupon.deletedInd   = True
    coupon.modifiedDate = datetime.now()
    db.commit()
    return True


# ─────────────────────────────────────────────
# MARK DIGITAL COUPON AS USED
# ─────────────────────────────────────────────
def mark_digital_coupon_used(
    db: Session,
    coupon_id: int,
    user_profile_id: str,
    order_id: Optional[int] = None,
):
    """
    Idempotent – calling twice for the same user+coupon is a no-op.
    Returns True on success, raises HTTPException on invalid coupon.
    """
    coupon = get_coupon_by_id(db, coupon_id)
    if not coupon:
        raise HTTPException(404, "Coupon not found")

    # Check already used
    existing = db.query(CouponUsage).filter(
        CouponUsage.couponId      == coupon_id,
        CouponUsage.userProfileId == user_profile_id,
    ).first()

    if existing:
        return True   # already marked – idempotent

    usage = CouponUsage(
        couponId      = coupon_id,
        userProfileId = user_profile_id,
        usedAt        = datetime.now(timezone.utc),
        orderId       = order_id,
    )
    db.add(usage)
    db.commit()
    return True


# ─────────────────────────────────────────────
# PHYSICAL COUPON – VALIDATE
# ─────────────────────────────────────────────
def validate_physical_coupon(db: Session, coupon_code: str):
    """
    Returns the PhysicalCoupon row if the code is valid and unused.
    Returns None if not found.
    Raises HTTPException(409) if already used.
    """
    code   = coupon_code.strip().upper()
    coupon = db.query(PhysicalCoupon).filter(
        PhysicalCoupon.couponCode == code
    ).first()

    if not coupon:
        return None

    if coupon.isUsed:
        raise HTTPException(409, "This coupon has already been used")

    return coupon


# ─────────────────────────────────────────────
# PHYSICAL COUPON – MARK USED
# ─────────────────────────────────────────────
def mark_physical_coupon_used(
    db: Session,
    coupon_code: str,
    user_profile_id: str,
    order_id: Optional[int] = None,
):
    code   = coupon_code.strip().upper()
    coupon = db.query(PhysicalCoupon).filter(
        PhysicalCoupon.couponCode == code
    ).first()

    if not coupon:
        raise HTTPException(404, "Physical coupon not found")

    if coupon.isUsed:
        raise HTTPException(409, "This coupon has already been used")

    coupon.isUsed              = True
    coupon.usedByUserProfileId = user_profile_id
    coupon.usedAt              = datetime.now(timezone.utc)
    coupon.orderId             = order_id
    db.commit()
    db.refresh(coupon)
    return coupon


# ─────────────────────────────────────────────
# PHYSICAL COUPON – BULK CREATE (Excel upload)
# ─────────────────────────────────────────────
def bulk_create_physical_coupons(
    db: Session,
    codes: List[dict],   # list of {"couponCode": str, "discount": int|None, "description": str|None}
    organization_id: Optional[int] = None,
):
    """
    Inserts physical coupons, skipping duplicates silently.
    Returns (inserted_count, skipped_count).
    """
    now       = datetime.now(timezone.utc)
    inserted  = 0
    skipped   = 0

    existing_codes = {
        r.couponCode
        for r in db.query(PhysicalCoupon.couponCode).all()
    }

    for row in codes:
        code = (row.get("couponCode") or "").strip().upper()
        if not code:
            skipped += 1
            continue
        if code in existing_codes:
            skipped += 1
            continue

        db.add(PhysicalCoupon(
            couponCode     = code,
            discount       = row.get("discount"),
            description    = row.get("description"),
            isUsed         = False,
            createdDate    = now,
            organizationId = organization_id,
        ))
        existing_codes.add(code)
        inserted += 1

    db.commit()
    return inserted, skipped


# ─────────────────────────────────────────────
# PHYSICAL COUPON – GET ALL (admin)
# ─────────────────────────────────────────────
def get_all_physical_coupons(
    db: Session,
    page_index: Optional[int] = None,
    page_size:  Optional[int] = None,
):
    query = db.query(PhysicalCoupon).order_by(PhysicalCoupon.physicalCouponId.desc())
    total = query.count()

    if page_index and page_size:
        offset = (page_index - 1) * page_size
        query  = query.offset(offset).limit(page_size)

    return {"list": query.all(), "count": total}