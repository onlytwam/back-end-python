"""

app/images/router.py

─────────────────────

Single module that serves ALL page images for the frontend.



Public endpoints (no auth required):

  GET  /api/images            → list every registered page image (admin: all; includes isActive)

  GET  /api/images/{page_key} → single image by key (includes isActive)



Admin endpoints (SUPER_ADMIN only):

  PUT    /api/images/{page_key}            → upload / replace a page image

  DELETE /api/images/{page_key}            → clear image URL (remove from slot)

  PATCH  /api/images/{page_key}/visibility → hide or show a home section

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

    clear_image,

    set_visibility,

)

from app.images.schemas import (

    PageImageResponse,

    PageImagesListResponse,

    UploadPageImageResponse,

    DeletePageImageResponse,

    VisibilityUpdateRequest,

    VisibilityUpdateResponse,

)

from app.shared.azure_storage import delete_image, upload_image, upload_media



router = APIRouter(prefix="/api/images", tags=["Images"])





# ── helpers ────────────────────────────────────────────────────────────────



def _resolve_url(url: str, request: Request = None) -> str:
    """Resolve any stored path to a full URL using url_helpers."""
    from app.shared.url_helpers import resolve_url
    return resolve_url(url) or ""





def _row_to_response(row, request: Request) -> PageImageResponse:

    return PageImageResponse(

        pageKey=row.pageKey,

        imageUrl=_resolve_url(row.imageUrl or "", request),

        description=row.description,

        isActive=bool(row.isActive),

    )





# ── Public: list all (admin consumes this to manage slots) ─────────────────



@router.get("", response_model=PageImagesListResponse)

def list_page_images(request: Request, db: Session = Depends(get_db)):

    """Return all registered page images, including hidden ones (isActive=False)."""

    rows = get_all(db)

    return PageImagesListResponse(

        images=[_row_to_response(r, request) for r in rows]

    )





# ── Public: single key ─────────────────────────────────────────────────────



@router.get("/{page_key}", response_model=PageImageResponse)

def get_page_image(page_key: str, request: Request, db: Session = Depends(get_db)):

    """Return the image URL + visibility for a specific page slot."""

    row = get_by_key(db, page_key)

    if not row:

        fallback, desc = PAGE_IMAGE_DEFAULTS.get(page_key, ("", ""))

        return PageImageResponse(

            pageKey=page_key,

            imageUrl=_resolve_url(fallback, request),

            description=desc,

            isActive=True,

        )

    return _row_to_response(row, request)





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

    """Upload a new image/video for the given page slot (admin only)."""

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



    # Derive the page-level subfolder from the key prefix so files are grouped

    # by page inside the container.

    #   login_background      → page_images/login/filename.jpg

    #   home_hero_desktop     → page_images/home/filename.jpg

    #   home_section_twam     → page_images/home/filename.jpg

    # Future keys follow the same convention: <page>_<slot> → folder = <page>

    page_folder = page_key.split("_")[0]   # e.g. "home", "login"



    new_url = upload_media(file, container_key="home", folder=page_folder)

    _, desc = PAGE_IMAGE_DEFAULTS[page_key]

    upsert(db, page_key, new_url, description=desc)



    if old_url and old_url != new_url:

        delete_image(old_url)



    return UploadPageImageResponse(

        success=True,

        pageKey=page_key,

        imageUrl=_resolve_url(new_url, request),

    )





# ── Admin: delete (clear) image URL ───────────────────────────────────────



@router.delete(

    "/{page_key}",

    response_model=DeletePageImageResponse,

    dependencies=[Depends(require_roles(Roles.SUPER_ADMIN))],

)

def delete_page_image(

    page_key: str,

    db: Session = Depends(get_db),

):

    """Clear the image/video for the given page slot (admin only)."""

    if page_key not in PAGE_IMAGE_DEFAULTS:

        raise HTTPException(

            status_code=400,

            detail=f"Unknown page key '{page_key}'.",

        )



    row, old_url = clear_image(db, page_key)



    # Delete the Azure blob (or local file) if one existed

    if old_url:

        delete_image(old_url)



    return DeletePageImageResponse(success=True, pageKey=page_key)





# ── Admin: toggle visibility (show / hide section on home page) ────────────



@router.patch(

    "/{page_key}/visibility",

    response_model=VisibilityUpdateResponse,

    dependencies=[Depends(require_roles(Roles.SUPER_ADMIN))],

)

def update_visibility(

    page_key: str,

    body: VisibilityUpdateRequest,

    db: Session = Depends(get_db),

):

    """Show or hide a home-page section by toggling isActive (admin only)."""

    if page_key not in PAGE_IMAGE_DEFAULTS:

        raise HTTPException(

            status_code=400,

            detail=f"Unknown page key '{page_key}'.",

        )



    row = set_visibility(db, page_key, body.isActive)

    if row is None:

        raise HTTPException(status_code=404, detail=f"Page key '{page_key}' not found.")



    return VisibilityUpdateResponse(

        success=True,

        pageKey=page_key,

        isActive=bool(row.isActive),

    )