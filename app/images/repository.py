"""
app/images/repository.py
─────────────────────────
DB operations for the PageImages table.

Seeding:
  Call seed_page_images(db) once at startup (from the same place that calls
  seed_defaults in app_settings.py).  It inserts a row for every known page
  key if that key does not yet exist.  Existing rows are never overwritten, so
  any admin-uploaded URL is preserved across restarts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.images.models import PageImage


# ── Known page keys ────────────────────────────────────────────────────────
# key → (initial_url, human description)
PAGE_IMAGE_DEFAULTS: dict[str, tuple[str, str]] = {
    "login_background": (
        "",
        "Login page background image",
    ),
    "home_hero_mobile": (
        "",
        "Home page hero banner (mobile)",
    ),
    "home_hero_desktop": (
        "",
        "Home page hero (desktop) — upload an image or a video (.mp4/.webm); auto-detected by extension",
    ),
    "home_section_twam": (
        "",
        "Home page TWAM featured-section background",
    ),
    "home_section_bestsellers": (
        "",
        "Home page Best Sellers section background",
    ),
    "home_section_newarrivals": (
        "",
        "Home page New Arrivals section background",
    ),
    "home_shop_all_products": (
        "",
        "Shop by Category — 'All Products' banner image",
    ),
    "home_shop_by_category": (
        "",
        "Shop by Category section — visibility toggle (isActive only, no image needed)",
    ),
}


# ── Public helpers ─────────────────────────────────────────────────────────

def get_by_key(db: Session, page_key: str) -> Optional[PageImage]:
    return (
        db.query(PageImage)
        .filter(PageImage.pageKey == page_key, PageImage.deletedInd == False)
        .first()
    )


def get_all(db: Session) -> list[PageImage]:
    """Return ALL rows (active and hidden) — used by admin panel."""
    return (
        db.query(PageImage)
        .filter(PageImage.deletedInd == False)
        .order_by(PageImage.pageKey)
        .all()
    )


def upsert(
    db: Session,
    page_key: str,
    image_url: str,
    description: Optional[str] = None,
) -> PageImage:
    now = datetime.now(timezone.utc)
    row = get_by_key(db, page_key)
    if row is None:
        row = PageImage(
            pageKey=page_key,
            imageUrl=image_url,
            description=description,
            isActive=True,
            createdDate=now,
            modifiedDate=now,
            deletedInd=False,
        )
        db.add(row)
    else:
        row.imageUrl = image_url
        row.modifiedDate = now
        if description and not row.description:
            row.description = description
    db.commit()
    db.refresh(row)
    return row


def clear_image(db: Session, page_key: str) -> tuple[Optional[PageImage], str]:
    """
    Clear the imageUrl for page_key (keeps the row, empties the URL).
    Returns (updated_row, old_url) so the caller can delete the Azure blob.
    """
    now = datetime.now(timezone.utc)
    row = get_by_key(db, page_key)
    if row is None:
        return None, ""
    old_url = row.imageUrl or ""
    row.imageUrl = ""
    row.modifiedDate = now
    db.commit()
    db.refresh(row)
    return row, old_url


def set_visibility(db: Session, page_key: str, is_active: bool) -> Optional[PageImage]:
    """Set isActive for page_key (show / hide the section on the home page)."""
    now = datetime.now(timezone.utc)
    row = get_by_key(db, page_key)
    if row is None:
        return None
    row.isActive = is_active
    row.modifiedDate = now
    db.commit()
    db.refresh(row)
    return row


def seed_page_images(db: Session) -> None:
    """
    Insert a row for every known page key that does not already exist.
    Safe to call on every startup — existing rows are never touched.
    """
    now = datetime.now(timezone.utc)
    for key, (fallback, desc) in PAGE_IMAGE_DEFAULTS.items():
        exists = (
            db.query(PageImage)
            .filter(PageImage.pageKey == key, PageImage.deletedInd == False)
            .first()
        )
        if exists is None:
            db.add(
                PageImage(
                    pageKey=key,
                    imageUrl=fallback,
                    description=desc,
                    isActive=True,
                    createdDate=now,
                    modifiedDate=now,
                    deletedInd=False,
                )
            )
    db.commit()