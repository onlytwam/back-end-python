import os
import json
import shutil
from uuid import uuid4
from typing import Optional, Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Form, UploadFile, File
from sqlalchemy.orm import Session

from database import get_db
from . import models, schemas, repository
from app.auth.dependencies import get_current_user

router = APIRouter(
    prefix="/api/Brand",   # Keep this as it is (matches your frontend)
    tags=["Brand"],
)

UPLOAD_DIR = "uploads/brand"   # Better to separate brand uploads
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ── GET ALL BRANDS ─────────────────────────────
@router.get("", response_model=schemas.BrandListResponse)
def get_brands(
    Filters:         Optional[str]  = Query(None, alias="Filters"),
    Order_Ascending: Optional[bool] = Query(None, alias="Order.Ascending"),
    Order_Property:  Optional[str]  = Query(None, alias="Order.Property"),
    Page_Index:      Optional[int]  = Query(1, alias="Page.Index", ge=1),
    Page_Size:       Optional[int]  = Query(20, alias="Page.Size", ge=1),
    db: Session = Depends(get_db),
):
    parsed_filters = None
    if Filters:
        try:
            parsed_filters = json.loads(Filters)
            if isinstance(parsed_filters, dict):
                parsed_filters = [parsed_filters]
        except:
            raise HTTPException(status_code=400, detail="Invalid Filters format.")

    result = repository.get_all_brands(
        db=db,
        filters=parsed_filters,
        order_ascending=Order_Ascending,
        order_property=Order_Property,
        page_index=Page_Index,
        page_size=Page_Size,
    )

    #  Correct handling
    return {
        "count": result["count"],
        "list": result["list"],
        "parameters": None
    }
# ── GET SINGLE BRAND ─────────────────────────────
@router.get("/{brand_id}", response_model=schemas.BrandResponse)
def get_brand(brand_id: int, db: Session = Depends(get_db)):
    brand = repository.get_brand_by_id(db, brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return brand


# ── CREATE BRAND ─────────────────────────────
@router.post("/", response_model=schemas.BrandResponse, status_code=201)
def create_brand(
    brandName:        Annotated[str, Form(...)],
    brandDescription: Annotated[Optional[str], Form()] = None,
    websiteUrl:       Annotated[Optional[str], Form()] = None,
    isActive:         Annotated[bool, Form()] = True,
    brandLogo:        Annotated[Optional[UploadFile], File()] = None,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),   # Keep auth
):
    brand_data = {
        "brandName":        brandName,
        "brandDescription": brandDescription,
        "websiteUrl":       websiteUrl,
        "isActive":         isActive,
    }

    # Handle Logo Upload with unique filename
    if brandLogo and brandLogo.filename:
        ext = brandLogo.filename.split(".")[-1].lower()
        filename = f"{uuid4()}.{ext}"
        file_path = os.path.join(UPLOAD_DIR, filename)

        with open(file_path, "wb") as f:
            shutil.copyfileobj(brandLogo.file, f)

        brand_data["brandLogo"] = f"/uploads/brand/{filename}"   # Store relative path

    created_brand = repository.create_brand(db, brand_data)
    return created_brand


# ── UPDATE BRAND ─────────────────────────────
@router.put("/{brand_id}", response_model=schemas.BrandResponse)
def update_brand(
    brand_id:         int,
    brandName:        Annotated[Optional[str], Form()] = None,
    brandDescription: Annotated[Optional[str], Form()] = None,
    websiteUrl:       Annotated[Optional[str], Form()] = None,
    isActive:         Annotated[Optional[bool], Form()] = None,
    brandLogo:        Annotated[Optional[UploadFile], File()] = None,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    existing = repository.get_brand_by_id(db, brand_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Brand not found")

    update_data: dict = {}
    if brandName is not None:        update_data["brandName"] = brandName
    if brandDescription is not None: update_data["brandDescription"] = brandDescription
    if websiteUrl is not None:       update_data["websiteUrl"] = websiteUrl
    if isActive is not None:         update_data["isActive"] = isActive

    # Handle new Logo upload (optional)
    if brandLogo and brandLogo.filename:
        ext = brandLogo.filename.split(".")[-1].lower()
        filename = f"{uuid4()}.{ext}"
        file_path = os.path.join(UPLOAD_DIR, filename)

        with open(file_path, "wb") as f:
            shutil.copyfileobj(brandLogo.file, f)

        update_data["brandLogo"] = f"/uploads/brand/{filename}"

    updated_brand = repository.update_brand(db, brand_id, update_data)
    return updated_brand


# ── DELETE BRAND ─────────────────────────────
@router.delete("/{brand_id}", status_code=204)
def delete_brand(brand_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    result = repository.delete_brand(db, brand_id)
    if not result:
        raise HTTPException(status_code=404, detail="Brand not found")
    return None