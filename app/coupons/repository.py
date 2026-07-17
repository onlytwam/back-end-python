"""
app/coupons/repository.py
─────────────────────────
All DB logic for coupons (digital, physical, user-specific bulk).
"""

import json
from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import or_, func, desc, asc, text
from typing import Optional, List
from datetime import datetime, timezone

from .models import Coupons, CouponUsage, PhysicalCoupon, UserCouponBatch

# Import models from other modules for segmentation cross-queries
from app.orders.models import Orders
from app.orderitems.models import OrderItems
from app.auth.models import IdentityUser


# ══════════════════════════════════════════════════════════════════════════════
# SEGMENT DEFINITIONS
# Each segment has: key, label, description, icon, supportsBoth
# "supportsBoth" = whether "bottom N" is meaningful for this segment
# ══════════════════════════════════════════════════════════════════════════════

SEGMENT_DEFINITIONS = [
    {
        "key":         "most_frequent",
        "label":       "Most Frequent Buyers",
        "description": "Customers ranked by total number of orders placed",
        "icon":        "🔁",
        "supportsBoth": True,
    },
    {
        "key":         "highest_billed",
        "label":       "Highest Total Spend",
        "description": "Customers ranked by cumulative amount spent across all orders",
        "icon":        "💰",
        "supportsBoth": True,
    },
    {
        "key":         "most_items",
        "label":       "Most Items Purchased",
        "description": "Customers who have ordered the largest total quantity of items",
        "icon":        "📦",
        "supportsBoth": True,
    },
    {
        "key":         "highest_aov",
        "label":       "Highest Avg Order Value",
        "description": "Customers with the highest average spend per order (min 2 orders)",
        "icon":        "📈",
        "supportsBoth": True,
    },
    {
        "key":         "one_time_buyers",
        "label":       "One-Time Buyers",
        "description": "Customers who placed exactly one order — win them back",
        "icon":        "1️⃣",
        "supportsBoth": False,
    },
    {
        "key":         "least_frequent",
        "label":       "Least Frequent Buyers",
        "description": "Active customers with the fewest orders — encourage re-engagement",
        "icon":        "🐢",
        "supportsBoth": False,
    },
    {
        "key":         "recent_buyers",
        "label":       "Recent Buyers (Last 30 Days)",
        "description": "Customers who placed an order within the last 30 days",
        "icon":        "🆕",
        "supportsBoth": False,
    },
    {
        "key":         "inactive_buyers",
        "label":       "Inactive Buyers (90+ Days)",
        "description": "Customers with no orders in the last 90 days — re-activate them",
        "icon":        "💤",
        "supportsBoth": False,
    },
    {
        "key":         "highest_single_order",
        "label":       "Biggest Single Order",
        "description": "Customers ranked by their single highest-value order",
        "icon":        "🏆",
        "supportsBoth": True,
    },
    {
        "key":         "most_returns",
        "label":       "Customers with Most Returns",
        "description": "Customers who have returned the most orders — offer goodwill coupons",
        "icon":        "↩️",
        "supportsBoth": False,
    },
]


# ══════════════════════════════════════════════════════════════════════════════
# SEGMENTATION: fetch users for a given segment
# ══════════════════════════════════════════════════════════════════════════════

def get_segment_users(
    db: Session,
    segment_key: str,
    direction: str,    # "top" | "bottom"
    count: int,
) -> List[dict]:
    """
    Returns up to `count` users for the requested segment as a list of dicts:
      { userId, name, email, phone, metricLabel }
    """
    if direction not in ("top", "bottom"):
        raise HTTPException(400, "direction must be 'top' or 'bottom'")
    if count < 1 or count > 5000:
        raise HTTPException(400, "count must be between 1 and 5000")

    order_dir = desc if direction == "top" else asc

    # ── Most Frequent Buyers ──────────────────────────────────────────────────
    if segment_key == "most_frequent":
        rows = (
            db.query(
                IdentityUser.Id.label("userId"),
                IdentityUser.FirstName.label("firstName"),
                IdentityUser.LastName.label("lastName"),
                IdentityUser.Email.label("email"),
                IdentityUser.PhoneNumber.label("phone"),
                func.count(Orders.orderId).label("metric"),
            )
            .join(Orders, Orders.userProfileId == IdentityUser.Id)
            .filter(
                Orders.deletedInd == False,
                Orders.state.notin_(["cancelled", "refunded"]),
                IdentityUser.DeletedInd == False,
            )
            .group_by(
                IdentityUser.Id,
                IdentityUser.FirstName,
                IdentityUser.LastName,
                IdentityUser.Email,
                IdentityUser.PhoneNumber,
            )
            .order_by(order_dir("metric"))
            .limit(count)
            .all()
        )
        return _fmt(rows, lambda r: f"{int(r.metric)} orders")

    # ── Highest Total Spend ───────────────────────────────────────────────────
    elif segment_key == "highest_billed":
        rows = (
            db.query(
                IdentityUser.Id.label("userId"),
                IdentityUser.FirstName.label("firstName"),
                IdentityUser.LastName.label("lastName"),
                IdentityUser.Email.label("email"),
                IdentityUser.PhoneNumber.label("phone"),
                func.sum(Orders.totalAmount).label("metric"),
            )
            .join(Orders, Orders.userProfileId == IdentityUser.Id)
            .filter(
                Orders.deletedInd == False,
                Orders.state.notin_(["cancelled", "refunded"]),
                IdentityUser.DeletedInd == False,
            )
            .group_by(
                IdentityUser.Id,
                IdentityUser.FirstName,
                IdentityUser.LastName,
                IdentityUser.Email,
                IdentityUser.PhoneNumber,
            )
            .order_by(order_dir("metric"))
            .limit(count)
            .all()
        )
        return _fmt(rows, lambda r: f"₹{float(r.metric or 0):,.2f} total spend")

    # ── Most Items Purchased ──────────────────────────────────────────────────
    elif segment_key == "most_items":
        rows = (
            db.query(
                IdentityUser.Id.label("userId"),
                IdentityUser.FirstName.label("firstName"),
                IdentityUser.LastName.label("lastName"),
                IdentityUser.Email.label("email"),
                IdentityUser.PhoneNumber.label("phone"),
                func.sum(OrderItems.Quantity).label("metric"),
            )
            .join(Orders, Orders.userProfileId == IdentityUser.Id)
            .join(OrderItems, OrderItems.OrderId == Orders.orderId)
            .filter(
                Orders.deletedInd == False,
                OrderItems.DeletedInd == False,
                Orders.state.notin_(["cancelled", "refunded"]),
                IdentityUser.DeletedInd == False,
            )
            .group_by(
                IdentityUser.Id,
                IdentityUser.FirstName,
                IdentityUser.LastName,
                IdentityUser.Email,
                IdentityUser.PhoneNumber,
            )
            .order_by(order_dir("metric"))
            .limit(count)
            .all()
        )
        return _fmt(rows, lambda r: f"{int(r.metric or 0)} items")

    # ── Highest Average Order Value ───────────────────────────────────────────
    elif segment_key == "highest_aov":
        rows = (
            db.query(
                IdentityUser.Id.label("userId"),
                IdentityUser.FirstName.label("firstName"),
                IdentityUser.LastName.label("lastName"),
                IdentityUser.Email.label("email"),
                IdentityUser.PhoneNumber.label("phone"),
                func.avg(Orders.totalAmount).label("metric"),
            )
            .join(Orders, Orders.userProfileId == IdentityUser.Id)
            .filter(
                Orders.deletedInd == False,
                Orders.state.notin_(["cancelled", "refunded"]),
                IdentityUser.DeletedInd == False,
            )
            .group_by(
                IdentityUser.Id,
                IdentityUser.FirstName,
                IdentityUser.LastName,
                IdentityUser.Email,
                IdentityUser.PhoneNumber,
            )
            .having(func.count(Orders.orderId) >= 2)
            .order_by(order_dir("metric"))
            .limit(count)
            .all()
        )
        return _fmt(rows, lambda r: f"₹{float(r.metric or 0):,.2f} avg order")

    # ── One-Time Buyers ───────────────────────────────────────────────────────
    elif segment_key == "one_time_buyers":
        rows = (
            db.query(
                IdentityUser.Id.label("userId"),
                IdentityUser.FirstName.label("firstName"),
                IdentityUser.LastName.label("lastName"),
                IdentityUser.Email.label("email"),
                IdentityUser.PhoneNumber.label("phone"),
                func.count(Orders.orderId).label("metric"),
            )
            .join(Orders, Orders.userProfileId == IdentityUser.Id)
            .filter(
                Orders.deletedInd == False,
                Orders.state.notin_(["cancelled", "refunded"]),
                IdentityUser.DeletedInd == False,
            )
            .group_by(
                IdentityUser.Id,
                IdentityUser.FirstName,
                IdentityUser.LastName,
                IdentityUser.Email,
                IdentityUser.PhoneNumber,
            )
            .having(func.count(Orders.orderId) == 1)
            .order_by(desc(func.max(Orders.orderDate)))
            .limit(count)
            .all()
        )
        return _fmt(rows, lambda r: "1 order only")

    # ── Least Frequent Buyers ─────────────────────────────────────────────────
    elif segment_key == "least_frequent":
        rows = (
            db.query(
                IdentityUser.Id.label("userId"),
                IdentityUser.FirstName.label("firstName"),
                IdentityUser.LastName.label("lastName"),
                IdentityUser.Email.label("email"),
                IdentityUser.PhoneNumber.label("phone"),
                func.count(Orders.orderId).label("metric"),
            )
            .join(Orders, Orders.userProfileId == IdentityUser.Id)
            .filter(
                Orders.deletedInd == False,
                Orders.state.notin_(["cancelled", "refunded"]),
                IdentityUser.DeletedInd == False,
            )
            .group_by(
                IdentityUser.Id,
                IdentityUser.FirstName,
                IdentityUser.LastName,
                IdentityUser.Email,
                IdentityUser.PhoneNumber,
            )
            .having(func.count(Orders.orderId) >= 2)
            .order_by(asc("metric"))
            .limit(count)
            .all()
        )
        return _fmt(rows, lambda r: f"{int(r.metric)} orders")

    # ── Recent Buyers (Last 30 Days) ──────────────────────────────────────────
    elif segment_key == "recent_buyers":
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        rows = (
            db.query(
                IdentityUser.Id.label("userId"),
                IdentityUser.FirstName.label("firstName"),
                IdentityUser.LastName.label("lastName"),
                IdentityUser.Email.label("email"),
                IdentityUser.PhoneNumber.label("phone"),
                func.max(Orders.orderDate).label("metric"),
            )
            .join(Orders, Orders.userProfileId == IdentityUser.Id)
            .filter(
                Orders.deletedInd == False,
                Orders.state.notin_(["cancelled", "refunded"]),
                Orders.orderDate >= cutoff,
                IdentityUser.DeletedInd == False,
            )
            .group_by(
                IdentityUser.Id,
                IdentityUser.FirstName,
                IdentityUser.LastName,
                IdentityUser.Email,
                IdentityUser.PhoneNumber,
            )
            .order_by(desc("metric"))
            .limit(count)
            .all()
        )
        return _fmt(rows, lambda r: f"Last order: {r.metric.strftime('%d %b %Y') if r.metric else '-'}")

    # ── Inactive Buyers (90+ Days No Order) ──────────────────────────────────
    elif segment_key == "inactive_buyers":
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        rows = (
            db.query(
                IdentityUser.Id.label("userId"),
                IdentityUser.FirstName.label("firstName"),
                IdentityUser.LastName.label("lastName"),
                IdentityUser.Email.label("email"),
                IdentityUser.PhoneNumber.label("phone"),
                func.max(Orders.orderDate).label("metric"),
            )
            .join(Orders, Orders.userProfileId == IdentityUser.Id)
            .filter(
                Orders.deletedInd == False,
                IdentityUser.DeletedInd == False,
            )
            .group_by(
                IdentityUser.Id,
                IdentityUser.FirstName,
                IdentityUser.LastName,
                IdentityUser.Email,
                IdentityUser.PhoneNumber,
            )
            .having(func.max(Orders.orderDate) < cutoff)
            .order_by(asc("metric"))
            .limit(count)
            .all()
        )
        return _fmt(rows, lambda r: f"Inactive since: {r.metric.strftime('%d %b %Y') if r.metric else '-'}")

    # ── Biggest Single Order ──────────────────────────────────────────────────
    elif segment_key == "highest_single_order":
        rows = (
            db.query(
                IdentityUser.Id.label("userId"),
                IdentityUser.FirstName.label("firstName"),
                IdentityUser.LastName.label("lastName"),
                IdentityUser.Email.label("email"),
                IdentityUser.PhoneNumber.label("phone"),
                func.max(Orders.totalAmount).label("metric"),
            )
            .join(Orders, Orders.userProfileId == IdentityUser.Id)
            .filter(
                Orders.deletedInd == False,
                Orders.state.notin_(["cancelled", "refunded"]),
                IdentityUser.DeletedInd == False,
            )
            .group_by(
                IdentityUser.Id,
                IdentityUser.FirstName,
                IdentityUser.LastName,
                IdentityUser.Email,
                IdentityUser.PhoneNumber,
            )
            .order_by(order_dir("metric"))
            .limit(count)
            .all()
        )
        return _fmt(rows, lambda r: f"₹{float(r.metric or 0):,.2f} best order")

    # ── Most Returns ──────────────────────────────────────────────────────────
    elif segment_key == "most_returns":
        rows = (
            db.query(
                IdentityUser.Id.label("userId"),
                IdentityUser.FirstName.label("firstName"),
                IdentityUser.LastName.label("lastName"),
                IdentityUser.Email.label("email"),
                IdentityUser.PhoneNumber.label("phone"),
                func.count(Orders.orderId).label("metric"),
            )
            .join(Orders, Orders.userProfileId == IdentityUser.Id)
            .filter(
                Orders.deletedInd == False,
                Orders.isReturn == True,
                IdentityUser.DeletedInd == False,
            )
            .group_by(
                IdentityUser.Id,
                IdentityUser.FirstName,
                IdentityUser.LastName,
                IdentityUser.Email,
                IdentityUser.PhoneNumber,
            )
            .order_by(desc("metric"))
            .limit(count)
            .all()
        )
        return _fmt(rows, lambda r: f"{int(r.metric)} returns")

    else:
        raise HTTPException(400, f"Unknown segment key: '{segment_key}'")


def _fmt(rows, metric_fn) -> List[dict]:
    """Format SQLAlchemy rows into user dicts."""
    result = []
    for r in rows:
        first = r.firstName or ""
        last  = r.lastName  or ""
        name  = f"{first} {last}".strip() or None
        result.append({
            "userId":      r.userId,
            "name":        name,
            "email":       r.email,
            "phone":       r.phone,
            "metricLabel": metric_fn(r),
        })
    return result


# ══════════════════════════════════════════════════════════════════════════════
# BULK USER-SPECIFIC COUPON ASSIGNMENT
# Creates one Coupons row per user + one UserCouponBatch audit row
# ══════════════════════════════════════════════════════════════════════════════

def bulk_assign_user_specific_coupons(
    db: Session,
    user_ids: List[str],
    coupon_data: dict,           # couponCode, couponName, description, discount, startDate, endDate
    source_type: str,            # "excel_upload" | "segment_generate"
    organization_id: Optional[int] = None,
    created_by: Optional[str] = None,
    segment_key: Optional[str] = None,
    segment_direction: Optional[str] = None,
) -> dict:
    """
    For each user_id in user_ids, inserts one Coupons row with
    couponType='user_specific', toUserProfileId=user_id.
    Also inserts a UserCouponBatch audit row.
    Returns { batchId, usersAssigned, usersSkipped }.
    """
    if not user_ids:
        raise HTTPException(400, "No user IDs provided")

    now = datetime.now(timezone.utc)

    # Validate dates
    for field in ("startDate", "endDate"):
        if isinstance(coupon_data.get(field), str):
            coupon_data[field] = datetime.fromisoformat(coupon_data[field])
    if coupon_data.get("startDate") and coupon_data["startDate"].date() < now.date():
        raise HTTPException(400, "Start date cannot be in the past")
    if coupon_data.get("endDate") and coupon_data["endDate"].date() < now.date():
        raise HTTPException(400, "End date cannot be in the past")
    if coupon_data.get("startDate") and coupon_data.get("endDate"):
        if coupon_data["endDate"] < coupon_data["startDate"]:
            raise HTTPException(400, "End date must be after start date")

    # Validate users exist
    valid_ids = {
        r.Id
        for r in db.query(IdentityUser.Id)
        .filter(IdentityUser.Id.in_(user_ids), IdentityUser.DeletedInd == False)
        .all()
    }
    skipped = len(user_ids) - len(valid_ids)

    # Create audit batch first (to get batchId)
    batch = UserCouponBatch(
        couponCode        = coupon_data.get("couponCode"),
        couponName        = coupon_data.get("couponName"),
        description       = coupon_data.get("description"),
        discount          = coupon_data.get("discount"),
        startDate         = coupon_data.get("startDate"),
        endDate           = coupon_data.get("endDate"),
        sourceType        = source_type,
        segmentKey        = segment_key,
        segmentDirection  = segment_direction,
        userCount         = len(valid_ids),
        userIdsSnapshot   = json.dumps(list(valid_ids)),
        organizationId    = organization_id,
        createdDate       = now,
        createdBy         = created_by,
        isActive          = True,
    )
    db.add(batch)
    db.flush()  # get batch.batchId without full commit

    # Insert one Coupons row per valid user
    for uid in valid_ids:
        coupon = Coupons(
            couponCode      = coupon_data.get("couponCode"),
            couponName      = coupon_data.get("couponName"),
            description     = coupon_data.get("description"),
            discount        = coupon_data.get("discount"),
            startDate       = coupon_data.get("startDate"),
            endDate         = coupon_data.get("endDate"),
            isActive        = True,
            isCommon        = False,
            couponType      = "user_specific",
            toUserProfileId = uid,
            createdDate     = now,
            createdBy       = created_by,
            deletedInd      = False,
            organizationId  = organization_id,
            batchId         = batch.batchId,
        )
        db.add(coupon)

    db.commit()
    db.refresh(batch)

    return {
        "batchId":       batch.batchId,
        "usersAssigned": len(valid_ids),
        "usersSkipped":  skipped,
    }


# ══════════════════════════════════════════════════════════════════════════════
# BATCH HISTORY (admin view)
# ══════════════════════════════════════════════════════════════════════════════

def get_user_coupon_batches(
    db: Session,
    page_index: Optional[int] = None,
    page_size:  Optional[int] = None,
) -> dict:
    query = db.query(UserCouponBatch).order_by(desc(UserCouponBatch.createdDate))
    total = query.count()
    if page_index and page_size:
        query = query.offset((page_index - 1) * page_size).limit(page_size)
    return {"list": query.all(), "count": total}


def deactivate_batch(db: Session, batch_id: int) -> bool:
    """
    Soft-deactivates all coupons in a batch + marks the batch inactive.
    Useful if admin made a mistake and wants to cancel the batch.
    """
    batch = db.query(UserCouponBatch).filter(UserCouponBatch.batchId == batch_id).first()
    if not batch:
        return False
    batch.isActive = False

    db.query(Coupons).filter(
        Coupons.batchId    == batch_id,
        Coupons.deletedInd == False,
    ).update({"isActive": False, "modifiedDate": datetime.now(timezone.utc)})

    db.commit()
    return True


# ══════════════════════════════════════════════════════════════════════════════
# EXISTING FUNCTIONS (unchanged)
# ══════════════════════════════════════════════════════════════════════════════

def _used_coupon_ids(db: Session, user_profile_id: str) -> set:
    rows = (
        db.query(CouponUsage.couponId)
        .filter(CouponUsage.userProfileId == user_profile_id)
        .all()
    )
    return {r.couponId for r in rows}


def get_all_coupons(db, filters=None, order_ascending=None, order_property=None, page_index=None, page_size=None):
    from app.shared.filters import apply_filters, apply_ordering
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
        query = query.offset((page_index - 1) * page_size).limit(page_size)
    return {"list": query.all(), "count": total}


def get_user_coupons(db, user_profile_id=None, filters=None, order_ascending=None, order_property=None, page_index=None, page_size=None):
    from app.shared.filters import apply_filters, apply_ordering
    now = datetime.now()
    query = db.query(Coupons).filter(Coupons.deletedInd == False, Coupons.isActive == True)
    if user_profile_id:
        query = query.filter(or_(Coupons.isCommon == True, Coupons.toUserProfileId == user_profile_id))
    else:
        query = query.filter(Coupons.isCommon == True)
    query = query.filter(or_(Coupons.endDate == None, Coupons.endDate >= now))
    if user_profile_id:
        used_ids = _used_coupon_ids(db, user_profile_id)
        if used_ids:
            query = query.filter(Coupons.couponId.notin_(used_ids))
    query = apply_filters(query, Coupons, filters)
    query = apply_ordering(query, Coupons, order_property, order_ascending)
    total   = query.count()
    if page_index and page_size:
        query = query.offset((page_index - 1) * page_size).limit(page_size)
    results = query.all()
    for coupon in results:
        coupon.isExpire = False
    return {"list": results, "count": total}


def get_coupon_by_id(db, coupon_id):
    return db.query(Coupons).filter(Coupons.couponId == coupon_id, Coupons.deletedInd == False).first()


def create_coupon(db, coupon_data):
    for field in ("startDate", "endDate"):
        if isinstance(coupon_data.get(field), str):
            coupon_data[field] = datetime.fromisoformat(coupon_data[field])
    now = datetime.now()
    if coupon_data.get("startDate") and coupon_data["startDate"].date() < now.date():
        raise HTTPException(400, "Start date cannot be in the past")
    if coupon_data.get("endDate") and coupon_data["endDate"].date() < now.date():
        raise HTTPException(400, "End date cannot be in the past")
    if coupon_data.get("startDate") and coupon_data.get("endDate"):
        if coupon_data["endDate"] < coupon_data["startDate"]:
            raise HTTPException(400, "End date must be after start date")
    if "isCommon" not in coupon_data or coupon_data.get("isCommon") is None:
        coupon_data["isCommon"] = coupon_data.get("couponType") == "generic"
    coupon = Coupons(**coupon_data, createdDate=datetime.now(), deletedInd=False)
    db.add(coupon)
    db.commit()
    db.refresh(coupon)
    return coupon


def update_coupon(db, coupon_id, coupon_data):
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


def delete_coupon(db, coupon_id):
    coupon = get_coupon_by_id(db, coupon_id)
    if not coupon:
        return None
    coupon.deletedInd   = True
    coupon.modifiedDate = datetime.now()
    db.commit()
    return True


def mark_digital_coupon_used(db, coupon_id, user_profile_id, order_id=None):
    coupon = get_coupon_by_id(db, coupon_id)
    if not coupon:
        raise HTTPException(404, "Coupon not found")
    existing = db.query(CouponUsage).filter(
        CouponUsage.couponId == coupon_id,
        CouponUsage.userProfileId == user_profile_id,
    ).first()
    if existing:
        return True
    usage = CouponUsage(
        couponId=coupon_id, userProfileId=user_profile_id,
        usedAt=datetime.now(timezone.utc), orderId=order_id,
    )
    db.add(usage)
    db.commit()
    return True


def validate_physical_coupon(db, coupon_code):
    code   = coupon_code.strip().upper()
    coupon = db.query(PhysicalCoupon).filter(PhysicalCoupon.couponCode == code).first()
    if not coupon:
        return None
    if coupon.isUsed:
        raise HTTPException(409, "This coupon has already been used")
    return coupon


def mark_physical_coupon_used(db, coupon_code, user_profile_id, order_id=None):
    code   = coupon_code.strip().upper()
    coupon = db.query(PhysicalCoupon).filter(PhysicalCoupon.couponCode == code).first()
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


def bulk_create_physical_coupons(db, codes, organization_id=None):
    now = datetime.now(timezone.utc)
    inserted = skipped = 0
    existing_codes = {r.couponCode for r in db.query(PhysicalCoupon.couponCode).all()}
    for row in codes:
        code = (row.get("couponCode") or "").strip().upper()
        if not code or code in existing_codes:
            skipped += 1
            continue
        db.add(PhysicalCoupon(
            couponCode=code, discount=row.get("discount"),
            description=row.get("description"), isUsed=False,
            createdDate=now, organizationId=organization_id,
        ))
        existing_codes.add(code)
        inserted += 1
    db.commit()
    return inserted, skipped


def get_all_physical_coupons(db, page_index=None, page_size=None):
    query = db.query(PhysicalCoupon).order_by(PhysicalCoupon.physicalCouponId.desc())
    total = query.count()
    if page_index and page_size:
        query = query.offset((page_index - 1) * page_size).limit(page_size)
    return {"list": query.all(), "count": total}

# ── Brand Coupon CRUD ──────────────────────────────────────────────────────────

def ensure_brand_coupon_columns(db):
    """
    Idempotent migration — adds BrandId / IsSingleUse / IsUsed columns to
    coupons."Coupons" if they don't exist yet. Safe to call on every startup.
    """
    from sqlalchemy import text
    for col, typedef in [
        ('"BrandId"',     "INTEGER"),
        ('"IsSingleUse"', "BOOLEAN DEFAULT FALSE"),
        ('"IsUsed"',      "BOOLEAN DEFAULT FALSE"),
    ]:
        try:
            db.execute(text(f'ALTER TABLE coupons."Coupons" ADD COLUMN IF NOT EXISTS {col} {typedef}'))
            db.commit()
        except Exception as exc:
            db.rollback()
            print(f"[BrandCoupon] Migration note for {col}: {exc}")


def create_brand_coupon(db, *, coupon_code: str, coupon_name: str, brand_id: int,
                         discount: int, start_date=None, end_date=None,
                         organization_id=None, created_by=None):
    """Creates a single brand-specific single-use coupon."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    existing = db.query(Coupons).filter(
        Coupons.couponCode == coupon_code.strip().upper(),
        Coupons.deletedInd == False,
    ).first()
    if existing:
        raise ValueError(f"Coupon code '{coupon_code}' already exists")

    c = Coupons(
        couponCode      = coupon_code.strip().upper(),
        couponName      = coupon_name,
        discount        = discount,
        startDate       = start_date,
        endDate         = end_date,
        isActive        = True,
        isCommon        = False,
        couponType      = "brand_specific",
        brandId         = brand_id,
        isSingleUse     = True,
        isUsed          = False,
        state           = "Approved",
        createdDate     = now,
        modifiedDate    = now,
        organizationId  = organization_id,
        createdBy       = created_by,
        deletedInd      = False,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def bulk_create_brand_coupons(db, *, codes: list, coupon_name: str, brand_id: int,
                               discount: int, start_date=None, end_date=None,
                               organization_id=None, created_by=None):
    """Bulk-creates brand coupons from a list of codes. Returns (inserted, skipped)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    existing_codes = {
        c[0] for c in db.query(Coupons.couponCode)
        .filter(Coupons.deletedInd == False).all()
    }
    inserted, skipped = 0, 0
    for raw_code in codes:
        code = (raw_code or "").strip().upper()
        if not code or code in existing_codes:
            skipped += 1
            continue
        db.add(Coupons(
            couponCode      = code,
            couponName      = coupon_name,
            discount        = discount,
            startDate       = start_date,
            endDate         = end_date,
            isActive        = True,
            isCommon        = False,
            couponType      = "brand_specific",
            brandId         = brand_id,
            isSingleUse     = True,
            isUsed          = False,
            state           = "Approved",
            createdDate     = now,
            modifiedDate    = now,
            organizationId  = organization_id,
            createdBy       = created_by,
            deletedInd      = False,
        ))
        existing_codes.add(code)
        inserted += 1
    db.commit()
    return inserted, skipped


def get_brand_coupons(db, brand_id=None, status=None, page_index=None, page_size=None):
    """
    status: None/"all" = all, "active" = active+unused, "used" = isUsed=True, "inactive" = isActive=False
    """
    query = (
        db.query(Coupons)
        .filter(Coupons.couponType == "brand_specific", Coupons.deletedInd == False)
    )
    if brand_id:
        query = query.filter(Coupons.brandId == brand_id)
    if status == "used":
        query = query.filter(Coupons.isUsed == True)
    elif status == "inactive":
        query = query.filter(Coupons.isActive == False, Coupons.isUsed == False)
    elif status == "active":
        query = query.filter(Coupons.isActive == True, Coupons.isUsed == False)
    query = query.order_by(Coupons.createdDate.desc())
    total = query.count()
    if page_index and page_size:
        query = query.offset((page_index - 1) * page_size).limit(page_size)
    return {"list": query.all(), "count": total}


def get_brand_coupon_usage_details(db, brand_id=None, page_index=None, page_size=None):
    """
    Returns used brand coupons with user email, used-at time, and order id.
    Joins CouponUsage → People for email, and Coupons for coupon details.
    """
    from sqlalchemy import text
    brand_filter = f'AND c."BrandId" = {int(brand_id)}' if brand_id else ""
    rows = db.execute(text(f"""
        SELECT
            c."CouponId", c."CouponCode", c."CouponName", c."Discount",
            c."BrandId",
            cu."UsageId", cu."UserProfileId", cu."UsedAt", cu."OrderId",
            p."EmailId"
        FROM coupons."Coupons" c
        INNER JOIN coupons."CouponUsage" cu ON cu."CouponId" = c."CouponId"
        LEFT JOIN twam."People" p ON p."UserProfileId" = cu."UserProfileId"
        WHERE c."CouponType" = 'brand_specific'
          AND c."DeletedInd" = false
          {brand_filter}
        ORDER BY cu."UsedAt" DESC
    """)).fetchall()

    total = len(rows)
    if page_index and page_size:
        start = (page_index - 1) * page_size
        rows  = rows[start : start + page_size]

    return {
        "count": total,
        "list": [
            {
                "couponId":     r[0],
                "couponCode":   r[1],
                "couponName":   r[2],
                "discount":     r[3],
                "brandId":      r[4],
                "usageId":      r[5],
                "userProfileId": r[6],
                "usedAt":       r[7].isoformat() if r[7] else None,
                "orderId":      r[8],
                "userEmail":    r[9] or "—",
            }
            for r in rows
        ],
    }


def validate_brand_coupon(db, *, coupon_code: str, brand_id: int, user_profile_id: str):
    """
    Validates a brand coupon:
      - Code must exist, be active, not deleted
      - Must be brand_specific type for the given brandId
      - Must not be already used (isSingleUse=True → isUsed must be False)
      - Validity dates must include today
      - User must not have used it before
    Returns dict with valid/discount/message.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    coupon = db.query(Coupons).filter(
        Coupons.couponCode == coupon_code.strip().upper(),
        Coupons.couponType == "brand_specific",
        Coupons.deletedInd == False,
        Coupons.isActive == True,
    ).first()

    if not coupon:
        return {"valid": False, "discount": None, "message": "Invalid coupon code"}

    if coupon.brandId != brand_id:
        return {"valid": False, "discount": None,
                "message": "This coupon is not valid for this brand"}

    if coupon.isSingleUse and coupon.isUsed:
        return {"valid": False, "discount": None,
                "message": "This coupon has already been used"}

    if coupon.startDate and coupon.startDate.replace(tzinfo=timezone.utc) > now:
        return {"valid": False, "discount": None,
                "message": "This coupon is not yet active"}

    if coupon.endDate and coupon.endDate.replace(tzinfo=timezone.utc) < now:
        return {"valid": False, "discount": None,
                "message": "This coupon has expired"}

    # Check if this user already used it
    usage = db.query(CouponUsage).filter(
        CouponUsage.couponId == coupon.couponId,
        CouponUsage.userProfileId == user_profile_id,
    ).first()
    if usage:
        return {"valid": False, "discount": None,
                "message": "You have already used this coupon"}

    return {
        "valid":     True,
        "couponId":  coupon.couponId,
        "discount":  coupon.discount,
        "brandId":   coupon.brandId,
        "couponName": coupon.couponName,
        "message":   f"{coupon.discount}% off on all {coupon.couponName or 'brand'} products",
    }


def mark_brand_coupon_used(db, *, coupon_id: int, user_profile_id: str, order_id=None):
    """Marks a brand coupon as used — sets isUsed=True on the Coupons row."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    coupon = db.query(Coupons).filter(Coupons.couponId == coupon_id).first()
    if not coupon:
        raise ValueError(f"Coupon {coupon_id} not found")

    db.add(CouponUsage(
        couponId=coupon_id, userProfileId=user_profile_id,
        usedAt=now, orderId=order_id,
    ))
    if coupon.isSingleUse:
        coupon.isUsed = True
        coupon.modifiedDate = now
        db.add(coupon)
    db.commit()


def get_admin_coupons_list(db, status=None, search=None, page_index=None, page_size=None):
    """
    Unified coupon list for the admin Coupon List page.
    Joins CouponUsage + People to surface who used each coupon.
    status: None/"all" | "active" | "used" | "inactive"
    search: filter by couponCode or couponName (case-insensitive)
    """
    from sqlalchemy import text

    status_filter = ""
    if status == "active":
        status_filter = 'AND c."IsActive" = true AND (c."IsUsed" IS NULL OR c."IsUsed" = false)'
    elif status == "used":
        status_filter = 'AND c."IsUsed" = true'
    elif status == "inactive":
        status_filter = 'AND c."IsActive" = false AND (c."IsUsed" IS NULL OR c."IsUsed" = false)'

    search_filter = ""
    search_param  = {}
    if search:
        search_filter = 'AND (c."CouponCode" ILIKE :s OR c."CouponName" ILIKE :s)'
        search_param  = {"s": f"%{search}%"}

    # Count query
    count_row = db.execute(text(f"""
        SELECT COUNT(*) FROM coupons."Coupons" c
        WHERE c."DeletedInd" = false
          {status_filter}
          {search_filter}
    """), search_param).scalar()

    # Main query — left join CouponUsage + People for used-by info
    # Also left join mdm.Brand for brand name on brand_specific coupons
    params = {**search_param}
    rows = db.execute(text(f"""
        SELECT
            c."CouponId",
            c."CouponCode",
            c."CouponName",
            c."CouponType",
            c."Discount",
            c."StartDate",
            c."EndDate",
            c."IsActive",
            c."IsUsed",
            c."BrandId",
            b."brandName",
            cu."UsedAt",
            cu."OrderId",
            p."EmailId"
        FROM coupons."Coupons" c
        LEFT JOIN coupons."CouponUsage" cu ON cu."CouponId" = c."CouponId"
        LEFT JOIN twam."People" p          ON p."UserProfileId" = cu."UserProfileId"
        LEFT JOIN mdm."Brand" b            ON b."brandId" = c."BrandId"
        WHERE c."DeletedInd" = false
          {status_filter}
          {search_filter}
        ORDER BY c."CreatedDate" DESC
    """), params).fetchall()

    total = count_row or 0
    if page_index and page_size:
        start = (page_index - 1) * page_size
        rows  = rows[start : start + page_size]

    return {
        "count": total,
        "list": [
            {
                "couponId":    r[0],
                "couponCode":  r[1],
                "couponName":  r[2],
                "couponType":  r[3],
                "discount":    r[4],
                "startDate":   r[5].isoformat() if r[5] else None,
                "endDate":     r[6].isoformat() if r[6] else None,
                "isActive":    r[7],
                "isUsed":      bool(r[8]),
                "brandId":     r[9],
                "brandName":   r[10],
                "usedAt":      r[11].isoformat() if r[11] else None,
                "usedOrderId": r[12],
                "usedByEmail": r[13] or None,
            }
            for r in rows
        ],
    }