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
# Initial URL is empty — admin must upload via /api/images/{page_key}.
# The app renders nothing (or a neutral placeholder) until an image is uploaded.
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
}


# ── Public helpers ─────────────────────────────────────────────────────────

def get_by_key(db: Session, page_key: str) -> Optional[PageImage]:
    return (
        db.query(PageImage)
        .filter(PageImage.pageKey == page_key, PageImage.deletedInd == False)
        .first()
    )


def get_all(db: Session) -> list[PageImage]:
    return (
        db.query(PageImage)
        .filter(PageImage.isActive == True, PageImage.deletedInd == False)
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