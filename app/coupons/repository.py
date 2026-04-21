from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import or_
from app.shared.filters import apply_filters, apply_ordering
from typing import Optional, List
from datetime import datetime, timezone
from .models import Coupons


# ─────────────────────────────────────────────
# GET ALL COUPONS
# ─────────────────────────────────────────────
def get_all_coupons(
    db: Session,
    filters: Optional[List[dict]] = None,
    order_ascending: Optional[bool] = None,
    order_property: Optional[str] = None,
    page_index: Optional[int] = None,
    page_size: Optional[int] = None,
):
    query = db.query(Coupons).filter(Coupons.deletedInd == False)

    # Handle case-insensitive coupon code filtering
    if filters:
        code_filters = [
            f for f in filters if f.get("property", "").lower() == "couponcode"
        ]
        other_filters = [
            f for f in filters if f.get("property", "").lower() != "couponcode"
        ]

        for cf in code_filters:
            query = query.filter(
                Coupons.couponCode.ilike(cf.get("value", ""))
            )

        filters = other_filters if other_filters else None

    query = apply_filters(query, Coupons, filters)
    query = apply_ordering(query, Coupons, order_property, order_ascending)

    # Pagination
    if page_index and page_size:
        offset = (page_index - 1) * page_size
        query = query.offset(offset).limit(page_size)

    total = query.count()

    if page_index and page_size:
        offset = (page_index - 1) * page_size
        query = query.offset(offset).limit(page_size)

    data = query.all()

    return {
        "list": data,
        "count": total,
    }


# ─────────────────────────────────────────────
# GET USER COUPONS
# ─────────────────────────────────────────────
def get_user_coupons(
    db: Session,
    user_profile_id: Optional[str] = None,
    filters: Optional[List[dict]] = None,
    order_ascending: Optional[bool] = None,
    order_property: Optional[str] = None,
    page_index: Optional[int] = None,
    page_size: Optional[int] = None,
):
    now = datetime.now()

    # Base query: active + not deleted
    query = db.query(Coupons).filter(
        Coupons.deletedInd == False,
        Coupons.isActive == True,
    )

    # Include common + user-specific coupons
    query = db.query(Coupons).filter(
    Coupons.deletedInd == False,
    Coupons.isActive == True,
    )

    if user_profile_id:
        query = query.filter(
        or_(
            Coupons.isCommon == True,
            Coupons.toUserProfileId == user_profile_id
        )
    )
    else:
        query = query.filter(
        Coupons.isCommon == True
        )

    # Exclude expired coupons
    query = query.filter(
        or_(
            Coupons.endDate == None,
            Coupons.endDate >= now,
        )
    )

    # Apply filters and ordering
    query = apply_filters(query, Coupons, filters)
    query = apply_ordering(query, Coupons, order_property, order_ascending)

    # Pagination
    total = query.count()

    if page_index and page_size:
        offset = (page_index - 1) * page_size
        query = query.offset(offset).limit(page_size)

    results = query.all()

    # Add computed flag for frontend safety
    for coupon in results:
        coupon.isExpire = False

    return {
        "list": results,
        "count": total,
    }


# ─────────────────────────────────────────────
# GET COUPON BY ID
# ─────────────────────────────────────────────
def get_coupon_by_id(db: Session, coupon_id: int):
    return db.query(Coupons).filter(
        Coupons.couponId == coupon_id,
        Coupons.deletedInd == False,
    ).first()


# ─────────────────────────────────────────────
# CREATE COUPON
# ─────────────────────────────────────────────
def create_coupon(db: Session, coupon_data: dict):

    # Convert string dates to datetime
    if isinstance(coupon_data.get("startDate"), str):
        coupon_data["startDate"] = datetime.fromisoformat(coupon_data["startDate"])

    if isinstance(coupon_data.get("endDate"), str):
        coupon_data["endDate"] = datetime.fromisoformat(coupon_data["endDate"])

    now = datetime.now()

    # Validation
    if coupon_data.get("startDate") and coupon_data["startDate"].date() < now.date():        
        raise HTTPException(400, "Start date cannot be in the past")

    if coupon_data.get("endDate") and coupon_data["endDate"].date() < now.date():
        raise HTTPException(400, "End date cannot be in the past")

    if coupon_data.get("startDate") and coupon_data.get("endDate"):
        if coupon_data["endDate"] < coupon_data["startDate"]:
            raise HTTPException(400, "End date must be after start date")

    # Persist coupon
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

    # Convert string → datetime
    if isinstance(coupon_data.get("startDate"), str):
        coupon_data["startDate"] = datetime.fromisoformat(coupon_data["startDate"])

    if isinstance(coupon_data.get("endDate"), str):
        coupon_data["endDate"] = datetime.fromisoformat(coupon_data["endDate"])

    # Normalize timezone
    if coupon_data.get("startDate") and coupon_data["startDate"].tzinfo is None:
        coupon_data["startDate"] = coupon_data["startDate"].replace(tzinfo=timezone.utc)

    if coupon_data.get("endDate") and coupon_data["endDate"].tzinfo is None:
        coupon_data["endDate"] = coupon_data["endDate"].replace(tzinfo=timezone.utc)

    #  FIX: compare only DATE
    if "startDate" in coupon_data and coupon_data["startDate"]:
        if coupon_data["startDate"].date() < now.date():
            raise HTTPException(400, "Start date cannot be in the past")

    if "endDate" in coupon_data and coupon_data["endDate"]:
        if coupon_data["endDate"].date() < now.date():
            raise HTTPException(400, "End date cannot be in the past")

    if coupon_data.get("startDate") and coupon_data.get("endDate"):
        if coupon_data["endDate"] < coupon_data["startDate"]:
            raise HTTPException(400, "End date must be after start date")

    # Update
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

    coupon.deletedInd = True
    coupon.modifiedDate = datetime.now()

    db.commit()

    return True