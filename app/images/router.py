"""
app/images/router.py
─────────────────────
Single module that serves ALL page images for the frontend.

Public endpoints (no auth required):
  GET  /api/images            → list every registered page image
  GET  /api/images/{page_key} → single image by key

Admin endpoint (SUPER_ADMIN only):
  PUT  /api/images/{page_key} → upload / replace a page image

Image storage follows the same pattern used by the rest of the app:
  • Azure Blob Storage when AZURE_STORAGE_CONNECTION_STRING is set (deployed)
  • Local /uploads/... folder when the variable is absent (local dev)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from database import get_db
from app.auth.dependencies import require_roles, Roles
from app.images.repository import (
    PAGE_IMAGE_DEFAULTS,
    get_all,
    get_by_key,
    upsert,
)
from app.images.schemas import (
    PageImageResponse,
    PageImagesListResponse,
    UploadPageImageResponse,
)
from app.shared.azure_storage import delete_image, upload_image, upload_media

router = APIRouter(prefix="/api/images", tags=["Images"])


# ── helpers ────────────────────────────────────────────────────────────────

def _resolve_url(url: str, request: Request) -> str:
    """Prepend the server base-URL to local /uploads/ paths."""
    if url and url.startswith("/uploads/"):
        return str(request.base_url).rstrip("/") + url
    return url or ""


# ── Public: list all ───────────────────────────────────────────────────────

@router.get("", response_model=PageImagesListResponse)
def list_page_images(request: Request, db: Session = Depends(get_db)):
    """Return all registered page images."""
    rows = get_all(db)
    return PageImagesListResponse(
        images=[
            PageImageResponse(
                pageKey=r.pageKey,
                imageUrl=_resolve_url(r.imageUrl or "", request),
                description=r.description,
            )
            for r in rows
        ]
    )


# ── Public: single key ─────────────────────────────────────────────────────

@router.get("/{page_key}", response_model=PageImageResponse)
def get_page_image(page_key: str, request: Request, db: Session = Depends(get_db)):
    """Return the image URL for a specific page slot."""
    row = get_by_key(db, page_key)
    if not row:
        # No row yet — return empty imageUrl so the frontend renders nothing
        # until an admin uploads an image via the admin panel.
        fallback, desc = PAGE_IMAGE_DEFAULTS.get(page_key, ("", ""))
        return PageImageResponse(
            pageKey=page_key,
            imageUrl=_resolve_url(fallback, request),
            description=desc,
        )
    return PageImageResponse(
        pageKey=row.pageKey,
        imageUrl=_resolve_url(row.imageUrl or "", request),
        description=row.description,
    )


# ── Admin: upload / replace ────────────────────────────────────────────────

@router.put(
    "/{page_key}",
    response_model=UploadPageImageResponse,
    dependencies=[Depends(require_roles(Roles.SUPER_ADMIN))],
)
def upload_page_image(
    page_key: str,
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload a new image for the given page slot (admin only)."""
    if page_key not in PAGE_IMAGE_DEFAULTS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown page key '{page_key}'. "
                f"Allowed keys: {sorted(PAGE_IMAGE_DEFAULTS)}"
            ),
        )

    old_row = get_by_key(db, page_key)
    old_url = old_row.imageUrl if old_row else ""

    new_url = upload_media(file, container_key="home", folder=page_key)
    _, desc = PAGE_IMAGE_DEFAULTS[page_key]
    upsert(db, page_key, new_url, description=desc)

    # Clean up the old blob after the DB is committed
    if old_url and old_url != new_url:
        delete_image(old_url)

    return UploadPageImageResponse(
        success=True,
        pageKey=page_key,
        imageUrl=_resolve_url(new_url, request),
    )