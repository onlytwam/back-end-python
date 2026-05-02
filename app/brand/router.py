from __future__ import annotations

from email.mime import text
import json
import logging
import os
import shutil
from typing import Optional
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from database import get_db
from . import repository, schemas

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/Brand",
    tags=["Brand"],
    redirect_slashes=True,
)

UPLOAD_DIR = "uploads/brands"


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _save_upload(file: UploadFile, subfolder: str = "") -> str:
    """Persist an uploaded file and return its URL-safe relative path."""
    dest_dir = os.path.join(UPLOAD_DIR, subfolder)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, file.filename)
    with open(dest_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    # Return forward-slash URL path
    return "/" + dest_path.replace("\\", "/")


def _to_url(path: Optional[str]) -> Optional[str]:
    """Convert a stored filesystem path to a URL-safe path."""
    if not path:
        return None
    if path.startswith("/") or path.startswith("http"):
        return path
    return "/" + path.replace("\\", "/")


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


# ─────────────────────────────────────────────
# PUBLIC — list / detail
# ─────────────────────────────────────────────
@router.get(
    "/",
    response_model=schemas.BrandListResponse,
    summary="List brands (paginated)",
)
def list_brands(
    Filters: Optional[str] = Query(None, alias="Filters"),
    Order_Property: Optional[str] = Query(None, alias="Order.Property"),
    Order_Ascending: Optional[bool] = Query(None, alias="Order.Ascending"),
    Page_Index: Optional[int] = Query(1, alias="Page.Index", ge=1),
    Page_Size: Optional[int] = Query(10, alias="Page.Size", ge=1, le=1000),  
    db: Session = Depends(get_db), 
) -> schemas.BrandListResponse:
    result = repository.get_all_brands(
        db              = db,
        filters         = _parse_filters(Filters),
        order_property  = Order_Property,
        order_ascending = Order_Ascending,
        page_index      = Page_Index,
        page_size       = Page_Size,
    )

    response_list = [
        schemas.BrandResponse(
            brandId          = b.brandId,
            brandName        = b.brandName,
            brandDescription = b.brandDescription,
            brandLogo        = _to_url(b.brandLogo),
            brandImage       = _to_url(b.brandImage),
            userProfileId    = b.userProfileId,
            state            = b.state,
            isActive         = b.isActive if b.isActive is not None else True,
        )
        for b in result["list"]
    ]

    return schemas.BrandListResponse(
        count      = result["count"],
        list       = response_list,
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
    if not brand:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Brand not found")
    return brand


# ─────────────────────────────────────────────
# PROTECTED — create / update / delete
# ─────────────────────────────────────────────

@router.post(
    "/",
    response_model=schemas.BrandResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a brand",
    dependencies=[Depends(get_current_user)],
)
def create_brand(
    brandName:        str                  = Form(...),
    brandDescription: Optional[str]        = Form(None),
    userProfileId:    Optional[str]        = Form(None),
    state:            Optional[str]        = Form(None),
    isActive:         Optional[str]        = Form("true"),
    brandLogo:        Optional[UploadFile] = File(None),
    brandImage:       Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
) -> schemas.BrandResponse:
    # Validate via schema (raises 422 with clear message on failure)
    validated = schemas.BrandCreate(
        brandName        = brandName,
        brandDescription = brandDescription,
        userProfileId    = userProfileId,
        state            = state,
        isActive         = _coerce_bool(isActive),
    )

    data = validated.model_dump(exclude={"brandLogoFileModel", "brandImageFileModel"})

    if brandLogo and brandLogo.filename:
        data["brandLogo"] = _save_upload(brandLogo)
        logger.info("Saved brand logo: %s", data["brandLogo"])

    if brandImage and brandImage.filename:
        data["brandImage"] = _save_upload(brandImage)
        logger.info("Saved brand image: %s", data["brandImage"])

    brand = repository.create_brand(db, data)
    logger.info("Created brand id=%s name=%r", brand.brandId, brand.brandName)
    return brand


@router.put(
    "/{brand_id}",
    response_model=schemas.BrandResponse,
    summary="Update a brand",
    dependencies=[Depends(get_current_user)],
)
def update_brand(
    brand_id:         int,
    brandName:        Optional[str]        = Form(None),
    brandDescription: Optional[str]        = Form(None),
    userProfileId:    Optional[str]        = Form(None),
    state:            Optional[str]        = Form(None),
    isActive:         Optional[str]        = Form(None),
    brandLogo:        Optional[UploadFile] = File(None),
    brandImage:       Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
) -> schemas.BrandResponse:
    if not repository.get_brand_by_id(db, brand_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Brand not found")

    # Validate supplied fields through schema
    validated = schemas.BrandUpdate(
        brandName        = brandName,
        brandDescription = brandDescription,
        userProfileId    = userProfileId,
        state            = state,
        isActive         = _coerce_bool(isActive) if isActive is not None else None,
    )

    # Only include fields that were actually sent (not None)
    data = {
        k: v for k, v in validated.model_dump(
            exclude={"brandLogoFileModel", "brandImageFileModel"}
        ).items()
        if v is not None
    }

    if brandLogo and brandLogo.filename:
        data["brandLogo"] = _save_upload(brandLogo)
        logger.info("Updated brand logo: %s", data["brandLogo"])

    if brandImage and brandImage.filename:
        data["brandImage"] = _save_upload(brandImage)
        logger.info("Updated brand image: %s", data["brandImage"])

    logger.info("Updating brand id=%s with fields: %s", brand_id, list(data.keys()))

    updated = repository.update_brand(db, brand_id, data)
    return updated


@router.delete(
    "/{brand_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a brand",
    dependencies=[Depends(get_current_user)],
)
def delete_brand(
    brand_id: int,
    db: Session = Depends(get_db),
) -> None:
    if not repository.delete_brand(db, brand_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Brand not found")