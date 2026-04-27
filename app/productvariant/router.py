import json
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from database import get_db
from . import schemas, repository
import os, shutil, uuid
from fastapi import UploadFile, File, Form
from .models import ProductImage, ProductVariant
# from app.auth.dependencies import get_current_user, require_roles, Roles, CurrentUser

router = APIRouter(
    # dependencies=[Depends(get_current_user)],
    prefix="/api/ProductVariants",
     tags=["ProductVariants"])

UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")


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
    from .models import ProductImage, ProductVariant

    #  Validate variant
    variant = db.query(ProductVariant).filter(
        ProductVariant.productVariantId == variant_id
    ).first()

    if not variant:
        raise HTTPException(status_code=404, detail="Variant not found")

    product_id = variant.productId

    os.makedirs(UPLOAD_DIR, exist_ok=True)

    saved = []

    for f in files:
        ext = os.path.splitext(f.filename or "img.jpg")[1] or ".jpg"
        fname = f"{uuid.uuid4().hex}{ext}"
        disk_path = os.path.join(UPLOAD_DIR, fname)

        # Save file to disk
        with open(disk_path, "wb") as out:
            shutil.copyfileobj(f.file, out)

        file_path = f"/{UPLOAD_DIR}/{fname}"

        print(f"Saving image → Variant: {variant_id}, Product: {product_id}")

        # Save to DB
        img = ProductImage(
            productId=product_id,
            productVariantId=variant_id,
            fileName=fname,
            fileType=f.content_type or "image/jpeg",
            filePath=file_path,
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