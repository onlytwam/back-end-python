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

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
IMAGE_DIR = os.path.join(BASE_DIR, "image") 

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
    variant = db.query(ProductVariant).filter(
        ProductVariant.productVariantId == variant_id
    ).first()
    if not variant:
        raise HTTPException(status_code=404, detail="Variant not found")

    product = db.query(Products).filter(
        Products.productId == variant.productId
    ).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    product_folder = product.productCode.strip().replace(' ', '-').replace('/', '-')

    # ── Use absolute path ────────────────────────────────────────────────────
    upload_dir = os.path.join(IMAGE_DIR, product_folder)
    os.makedirs(upload_dir, exist_ok=True)

    print(f"[upload] Saving to absolute path: {upload_dir}")

    saved = []

    for f in files:
        original_name = (f.filename or "image.jpg").replace(' ', '-')
        fname = original_name
        disk_path = os.path.join(upload_dir, fname)

        if os.path.exists(disk_path):
            fname = f"{uuid.uuid4().hex[:8]}_{original_name}"
            disk_path = os.path.join(upload_dir, fname)

        with open(disk_path, "wb") as out:
            shutil.copyfileobj(f.file, out)

        # DB path stays as relative — used by frontend via getImageUrl()
        file_path = f"image/{product_folder}/{fname}"

        print(f"[upload] Saved → disk: {disk_path} | db: {file_path}")

        img = ProductImage(
            productId=variant.productId,
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

    return {"uploaded": len(saved), "images": saved}

@router.delete("/{variant_id}/images/{image_id}", status_code=200)
def delete_variant_image(variant_id: int, image_id: int, db: Session = Depends(get_db)):
    img = db.query(ProductImage).filter(
        ProductImage.productImageId == image_id,
        ProductImage.productVariantId == variant_id
    ).first()

    if not img:
        raise HTTPException(status_code=404, detail="Image not found")

    try:
        # ── Absolute path for disk deletion ──────────────────────────────
        relative = img.filePath.lstrip("/")          # image/BNB-01/file.jpg
        disk_path = os.path.join(BASE_DIR, relative) # /home/site/wwwroot/image/BNB-01/file.jpg
        if os.path.exists(disk_path):
            os.remove(disk_path)
            print(f"[delete] Removed: {disk_path}")
        else:
            print(f"[delete] File not found on disk: {disk_path}")
    except Exception as e:
        print(f"[delete] Warning: {e}")

    db.delete(img)
    db.commit()
    return {"deleted": image_id}