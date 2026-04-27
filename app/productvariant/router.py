import json
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from database import get_db
from . import schemas, repository
import os, shutil, uuid
from fastapi import UploadFile, File, Form
from .models import ProductImage, ProductVariant
from app.products.models import Products

# from app.auth.dependencies import get_current_user, require_roles, Roles, CurrentUser

router = APIRouter(
    # dependencies=[Depends(get_current_user)],
    prefix="/api/ProductVariants",
     tags=["ProductVariants"])

# UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")

def parse_filters(raw: Optional[str]) -> Optional[list]:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else [parsed]
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid Filters format.")


# ── GET /api/ProductVariants ──────────────────────────────────────────────────
@router.get("", response_model=schemas.ProductVariantListResponse)
def get_variants(
    Filters:         Optional[str]  = Query(None, alias="Filters"),
    Order_Ascending: Optional[bool] = Query(None, alias="Order.Ascending"),
    Order_Property:  Optional[str]  = Query(None, alias="Order.Property"),
    Page_Index:      Optional[int]  = Query(None, alias="Page.Index", ge=1),
    Page_Size:       Optional[int]  = Query(None, alias="Page.Size",  ge=1),
    db: Session = Depends(get_db)
):
    return repository.get_all_variants(
        db=db,
        filters=parse_filters(Filters),
        order_ascending=Order_Ascending,
        order_property=Order_Property,
        page_index=Page_Index,
        page_size=Page_Size,
    )


# ── GET /api/ProductVariants/{Id} ─────────────────────────────────────────────
@router.get("/{variant_id}", response_model=schemas.ProductVariantResponse)
def get_variant(variant_id: int, db: Session = Depends(get_db)):
    result = repository.get_variant_by_id(db, variant_id)
    if not result:
        raise HTTPException(status_code=404, detail="ProductVariant not found.")
    return result


# ── POST /api/ProductVariants ─────────────────────────────────────────────────
@router.post("", status_code=201)
def create_variant(command: schemas.ProductVariantCreate, db: Session = Depends(get_db)):
    new_id = repository.create_variant(db, command)
    return {"productVariantId": new_id}


# ── PUT /api/ProductVariants/{Id} ─────────────────────────────────────────────
@router.put("/{variant_id}")
def update_variant(variant_id: int, command: schemas.ProductVariantUpdate, db: Session = Depends(get_db)):
    result = repository.update_variant(db, variant_id, command)
    if not result:
        raise HTTPException(status_code=404, detail="ProductVariant not found.")
    return {"productVariantId": result}


# ── DELETE /api/ProductVariants/{Id} ─────────────────────────────────────────
@router.delete("/{variant_id}", status_code=204)
def delete_variant(variant_id: int, db: Session = Depends(get_db)):
    result = repository.delete_variant(db, variant_id)
    if not result:
        raise HTTPException(status_code=404, detail="ProductVariant not found.")

# ── Get Images ─────────────────────────────────────────
@router.get("/{variant_id}/images")
def get_variant_images(variant_id: int, db: Session = Depends(get_db)):
    from .models import ProductImage

    images = db.query(ProductImage).filter(
        ProductImage.productVariantId == variant_id
    ).all()

    # Debug log
    print(f"Variant ID: {variant_id}, Images found: {len(images)}")

    # Return clean response
    return [
        {
            "productImageId": img.productImageId,
            "productId": img.productId,
            "productVariantId": img.productVariantId,
            "fileName": img.fileName,
            "filePath": img.filePath,
            "fileType": img.fileType,
        }
        for img in images
    ]
# ─────────────────────────────────────────────────────────────
# UPLOAD IMAGES
# ─────────────────────────────────────────────────────────────
@router.post("/{variant_id}/images", status_code=201)
def upload_variant_images(
    variant_id: int,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db)
):

    # Validate variant
    variant = db.query(ProductVariant).filter(
        ProductVariant.productVariantId == variant_id
    ).first()
    if not variant:
        raise HTTPException(status_code=404, detail="Variant not found")

    # Get product to use its code as folder name
    product = db.query(Products).filter(
        Products.productId == variant.productId
    ).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    # Sanitize product code for use as folder name
    # e.g. "BNB 01" → "BNB-01", "TLCB 925" → "TLCB-925"
    product_folder = product.productCode.strip().replace(' ', '-').replace('/', '-')

    # Build directory: image/BNB-01/
    upload_dir = os.path.join("image", product_folder)
    os.makedirs(upload_dir, exist_ok=True)

    saved = []

    for f in files:
        # Keep original filename but sanitize it
        original_name = (f.filename or "image.jpg").replace(' ', '-')
        ext = os.path.splitext(original_name)[1] or ".jpg"
        
        # Use original name if provided, else generate unique name
        # to avoid collisions append short uuid if file already exists
        fname = original_name
        disk_path = os.path.join(upload_dir, fname)
        
        if os.path.exists(disk_path):
            # Avoid overwrite — prefix with short uuid
            fname = f"{uuid.uuid4().hex[:8]}_{original_name}"
            disk_path = os.path.join(upload_dir, fname)

        # Save to disk
        with open(disk_path, "wb") as out:
            shutil.copyfileobj(f.file, out)

        # Store as: image/BNB-01/beig-6.png  (no leading slash)
        file_path = f"image/{product_folder}/{fname}"

        print(f"Saving → {file_path} | Variant: {variant_id} | Product: {variant.productId}")

        # Save to DB
        img = ProductImage(
            productId=variant.productId,
            productVariantId=variant_id,
            fileName=fname,
            fileType=f.content_type or "image/jpeg",
            filePath=file_path,   # ← stored as image/BNB-01/beig-6.png
        )
        db.add(img)
        db.commit()
        db.refresh(img)

        saved.append({
            "productImageId": img.productImageId,
            "filePath": file_path
        })

    return {
        "uploaded": len(saved),
        "images": saved
    }


@router.delete("/{variant_id}/images/{image_id}", status_code=200)
def delete_variant_image(variant_id: int, image_id: int, db: Session = Depends(get_db)):
    from .models import ProductImage

    img = db.query(ProductImage).filter(
        ProductImage.productImageId == image_id,
        ProductImage.productVariantId == variant_id  # safety check
    ).first()

    if not img:
        raise HTTPException(status_code=404, detail="Image not found")

    # Delete file from disk
    try:
        disk_path = img.filePath.lstrip("/")
        if os.path.exists(disk_path):
            os.remove(disk_path)
    except Exception as e:
        print(f"Warning: could not delete file: {e}")

    db.delete(img)
    db.commit()

    return {"deleted": image_id}