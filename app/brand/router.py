from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from typing import Optional

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Query, Request,
    UploadFile, status,
)
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.shared.azure_storage import upload_image, delete_image
from database import get_db
from . import repository, schemas

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/Brand",
    tags=["Brand"],
    redirect_slashes=True,
)


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

from app.shared.url_helpers import resolve_url as _to_url


def _parse_filters(raw: Optional[str]) -> Optional[list[dict]]:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return [parsed] if isinstance(parsed, dict) else parsed
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Filters JSON. Expected array of {property, comparison, value}.",
        )


def _coerce_bool(value: Optional[str], default: bool = True) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() not in ("false", "0", "no")


def _brand_to_response(b) -> schemas.BrandResponse:
    return schemas.BrandResponse(
        brandId          = b.brandId,
        brandName        = b.brandName,
        brandDescription = b.brandDescription,
        brandImage       = _to_url(b.brandImage),
        userProfileId    = b.userProfileId,
        state            = b.state,
        isActive         = b.isActive if b.isActive is not None else True,
        deletedInd       = bool(b.deletedInd) if b.deletedInd is not None else False,
    )


# ─────────────────────────────────────────────
# PUBLIC — list (active + non-deleted only)
# ─────────────────────────────────────────────

@router.get(
    "/",
    response_model=schemas.BrandListResponse,
    summary="List brands (paginated). Public: active only. Admin with showAll=true: includes hidden.",
)
def list_brands(
    Filters:           Optional[str]  = Query(None, alias="Filters"),
    Order_Property:    Optional[str]  = Query(None, alias="Order.Property"),
    Order_Ascending:   Optional[bool] = Query(None, alias="Order.Ascending"),
    Page_Index:        Optional[int]  = Query(1,    alias="Page.Index", ge=1),
    Page_Size:         Optional[int]  = Query(10,   alias="Page.Size",  ge=1, le=1000),
    # Admin callers pass showAll=true to also see hidden (isActive=false) brands.
    # Deleted brands (deletedInd=true) are NEVER returned to anyone via this endpoint.
    showAll:           bool           = Query(False, alias="showAll"),
    # Public callers pass withProducts=true to exclude brands that have zero
    # active+approved products (e.g. TwamBrands page).
    withProducts:      bool           = Query(False, alias="withProducts"),
    db: Session = Depends(get_db),
) -> schemas.BrandListResponse:

    # Public view always active_only=True; admin can pass showAll=true
    active_only = not showAll

    result = repository.get_all_brands(
        db                 = db,
        filters            = _parse_filters(Filters),
        order_property     = Order_Property,
        order_ascending    = Order_Ascending,
        page_index         = Page_Index,
        page_size          = Page_Size,
        active_only        = active_only,
        with_products_only = withProducts,
    )

    return schemas.BrandListResponse(
        count      = result["count"],
        list       = [_brand_to_response(b) for b in result["list"]],
        parameters = None,
    )


@router.get(
    "/{brand_id}",
    response_model=schemas.BrandResponse,
    summary="Get a single brand",
)
def get_brand(
    brand_id: int,
    db: Session = Depends(get_db),
) -> schemas.BrandResponse:
    brand = repository.get_brand_by_id(db, brand_id)
    if not brand or brand.deletedInd:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Brand not found")
    return _brand_to_response(brand)


# ─────────────────────────────────────────────
# PROTECTED — create / update
# ─────────────────────────────────────────────

@router.post(
    "/",
    response_model=schemas.BrandResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a brand",
    dependencies=[Depends(get_current_user)],
)
def create_brand(
    brandName:        str            = Form(...),
    brandDescription: Optional[str] = Form(None),
    userProfileId:    Optional[str] = Form(None),
    state:            Optional[str] = Form(None),
    isActive:         Optional[str] = Form("true"),
    db: Session = Depends(get_db),
) -> schemas.BrandResponse:
    validated = schemas.BrandCreate(
        brandName        = brandName,
        brandDescription = brandDescription,
        userProfileId    = userProfileId,
        state            = state,
        isActive         = _coerce_bool(isActive),
    )
    brand = repository.create_brand(db, validated.model_dump())
    logger.info("Created brand id=%s name=%r", brand.brandId, brand.brandName)
    return _brand_to_response(brand)


@router.put(
    "/{brand_id}",
    response_model=schemas.BrandResponse,
    summary="Update a brand (name / description)",
    dependencies=[Depends(get_current_user)],
)
def update_brand(
    brand_id:         int,
    brandName:        Optional[str] = Form(None),
    brandDescription: Optional[str] = Form(None),
    userProfileId:    Optional[str] = Form(None),
    state:            Optional[str] = Form(None),
    isActive:         Optional[str] = Form(None),
    db: Session = Depends(get_db),
) -> schemas.BrandResponse:
    existing = repository.get_brand_by_id(db, brand_id)
    if not existing or existing.deletedInd:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Brand not found")

    validated = schemas.BrandUpdate(
        brandName        = brandName,
        brandDescription = brandDescription,
        userProfileId    = userProfileId,
        state            = state,
        isActive         = _coerce_bool(isActive) if isActive is not None else None,
    )

    data = {k: v for k, v in validated.model_dump().items() if v is not None}
    logger.info("Updating brand id=%s with fields: %s", brand_id, list(data.keys()))
    updated = repository.update_brand(db, brand_id, data)
    return _brand_to_response(updated)


# ─────────────────────────────────────────────
# PROTECTED — toggle visibility (hide / show)
# ─────────────────────────────────────────────

@router.patch(
    "/{brand_id}/toggle-visibility",
    response_model=schemas.BrandResponse,
    summary="Toggle brand visibility (hide / show). Pass ?cascadeProducts=true to also hide/show all brand products.",
    dependencies=[Depends(get_current_user)],
)
def toggle_brand_visibility(
    brand_id: int,
    cascade_products: bool = Query(False, alias="cascadeProducts"),
    db: Session = Depends(get_db),
) -> schemas.BrandResponse:
    brand = repository.toggle_brand_visibility(db, brand_id)
    if not brand:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Brand not found or has been deleted",
        )
    state = "visible" if brand.isActive else "hidden"
    logger.info("Brand %s toggled → %s (cascadeProducts=%s)", brand_id, state, cascade_products)
    if cascade_products:
        try:
            if not brand.isActive:
                from app.products.repository import deactivate_products_by_brand
                deactivate_products_by_brand(db, brand_id)
            else:
                from app.products.repository import reactivate_products_by_brand
                reactivate_products_by_brand(db, brand_id)
        except Exception:
            pass
    return _brand_to_response(brand)


# ─────────────────────────────────────────────
# IMAGE — upload / delete
# ─────────────────────────────────────────────

@router.put(
    "/{brand_id}/image",
    summary="Upload / replace the image for a brand (admin only)",
    dependencies=[Depends(get_current_user)],
)
def upload_brand_image(
    brand_id: int,
    request:  Request,
    file:     UploadFile = File(...),
    db:       Session    = Depends(get_db),
):
    brand = repository.get_brand_by_id(db, brand_id)
    if not brand or brand.deletedInd:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Brand not found")

    old_url = brand.brandImage or ""
    new_url = upload_image(file, container_key="brands", folder=f"brand_{brand_id}")

    # Store raw path — resolve_url() handles display at read time

    brand.brandImage   = new_url
    brand.modifiedDate = datetime.now(timezone.utc)
    db.commit()

    if old_url and old_url != new_url:
        delete_image(old_url)

    logger.info("Brand %s image updated → %s", brand_id, new_url)
    return {"success": True, "brandId": brand_id, "imageUrl": _to_url(new_url)}


@router.delete(
    "/{brand_id}/image",
    summary="Remove the image for a brand (admin only)",
    dependencies=[Depends(get_current_user)],
)
def delete_brand_image(
    brand_id: int,
    db:       Session = Depends(get_db),
):
    brand = repository.get_brand_by_id(db, brand_id)
    if not brand or brand.deletedInd:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Brand not found")

    old_url = brand.brandImage or ""
    brand.brandImage   = None
    brand.modifiedDate = datetime.now(timezone.utc)
    db.commit()

    if old_url:
        delete_image(old_url)

    logger.info("Brand %s image deleted (was %s)", brand_id, old_url or "empty")
    return {"success": True, "brandId": brand_id}


# ─────────────────────────────────────────────
# SOFT-DELETE brand
# ─────────────────────────────────────────────

@router.delete(
    "/{brand_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete a brand. Products keep their brandId FK but brand becomes invisible.",
    dependencies=[Depends(get_current_user)],
)
def delete_brand(
    brand_id: int,
    db: Session = Depends(get_db),
) -> None:
    existing = repository.get_brand_by_id(db, brand_id)
    if not existing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Brand not found")

    if not repository.delete_brand(db, brand_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Brand not found")

    # NOTE: We intentionally do NOT delete the brand image on soft-delete
    # so it can be restored later. Image is only purged on hard-delete (future).
    logger.info("Brand %s soft-deleted", brand_id)
    # Cascade: set all products of this brand to isActive=False
    try:
        from app.products.repository import deactivate_products_by_brand
        deactivate_products_by_brand(db, brand_id)
    except Exception:
        pass
