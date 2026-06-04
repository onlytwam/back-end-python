import json
import os
import re
import uuid
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, File, UploadFile
from sqlalchemy.orm import Session

from database import get_db
from app.auth.dependencies import get_current_user, Roles
from . import schemas, repository
from .models import ProductImage, ProductVariant
from app.products.models import Products
from app.shared.azure_storage import upload_image, delete_image, AZURE_BASE_URL, CONTAINERS
from app.shared.url_helpers import resolve_url

MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB

router = APIRouter(
    prefix="/api/ProductVariants",
    tags=["ProductVariants"]
)


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def parse_filters(raw: Optional[str]) -> Optional[list]:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else [parsed]
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid Filters format.")


def sanitize_folder_name(name: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_-]', '', name or "default")


# ─────────────────────────────────────────────────────────────
# CRUD APIs
# ─────────────────────────────────────────────────────────────

@router.get("", response_model=schemas.ProductVariantListResponse)
def get_variants(
    Filters: Optional[str] = Query(None, alias="Filters"),
    Order_Ascending: Optional[bool] = Query(None, alias="Order.Ascending"),
    Order_Property: Optional[str] = Query(None, alias="Order.Property"),
    Page_Index: Optional[int] = Query(None, alias="Page.Index", ge=1),
    Page_Size: Optional[int] = Query(None, alias="Page.Size", ge=1),
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


@router.get("/{variant_id}", response_model=schemas.ProductVariantResponse)
def get_variant(variant_id: int, db: Session = Depends(get_db)):
    result = repository.get_variant_by_id(db, variant_id)
    if not result:
        raise HTTPException(status_code=404, detail="ProductVariant not found.")
    return result


@router.post("", status_code=201)
def create_variant(command: schemas.ProductVariantCreate, db: Session = Depends(get_db)):
    new_id = repository.create_variant(db, command)
    # ── Audit log ─────────────────────────────────────────────────────────────
    try:
        from app.stocklog.repository import add_audit_event as add_event
        add_event(
            db,
            product_id         = getattr(command, "productId", 0),
            product_variant_id = new_id,
            event_type         = "VARIANT_ADDED",
            event_source       = "ProductDetail",
            description        = f"Variant added: {getattr(command, 'variantName', '') or ''} [{getattr(command, 'color', '') or ''}]",
            changed_by         = getattr(command, "createdBy", None) or "Admin",
            product_code       = getattr(command, "productCode", None),
            variant_name       = getattr(command, "variantName", None),
        )
    except Exception:
        pass
    return {"productVariantId": new_id}


@router.put("/{variant_id}")
def update_variant(variant_id: int, command: schemas.ProductVariantUpdate, db: Session = Depends(get_db)):
    result = repository.update_variant(db, variant_id, command)
    if not result:
        raise HTTPException(status_code=404, detail="ProductVariant not found.")
    return {"productVariantId": result}


@router.delete("/{variant_id}", status_code=204)
def delete_variant(variant_id: int, db: Session = Depends(get_db)):
    # Snapshot before deletion for audit log
    variant_snap = None
    try:
        variant_snap = repository.get_variant_by_id(db, variant_id)
    except Exception:
        pass
    result = repository.delete_variant(db, variant_id)
    if not result:
        raise HTTPException(status_code=404, detail="ProductVariant not found.")
    # ── Audit log ─────────────────────────────────────────────────────────────
    try:
        from app.stocklog.repository import add_audit_event as add_event
        add_event(
            db,
            product_id         = variant_snap.get("productId", 0) if variant_snap else 0,
            product_variant_id = variant_id,
            event_type         = "VARIANT_DELETED",
            event_source       = "ProductDetail",
            description        = f"Variant deleted: {variant_snap.get('variantName','') if variant_snap else ''} [ID {variant_id}]",
            changed_by         = "Admin",
            variant_name       = variant_snap.get("variantName") if variant_snap else None,
        )
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# GET IMAGES
# ─────────────────────────────────────────────────────────────


# ── toggle variant active/inactive (isActive only — NOT deletedInd) ──────────
@router.patch("/{variant_id}/toggle-active", dependencies=[Depends(get_current_user)])
def toggle_variant_active(variant_id: int, db: Session = Depends(get_db)):
    from datetime import datetime, timezone
    from sqlalchemy import or_
    v = db.query(ProductVariant).filter(
        ProductVariant.productVariantId == variant_id,
        or_(ProductVariant.deletedInd == False, ProductVariant.deletedInd.is_(None)),
    ).first()
    if not v:
        raise HTTPException(status_code=404, detail="Variant not found.")
    # Treat NULL isActive as True (active), then toggle to False
    current_state  = v.isActive if v.isActive is not None else True
    new_state      = not current_state
    v.isActive     = new_state
    v.modifiedDate = datetime.now(timezone.utc)
    db.commit()
    return {"variantId": variant_id, "isActive": new_state}

@router.get("/{variant_id}/images")
def get_variant_images(variant_id: int, db: Session = Depends(get_db)):
    variant = db.query(ProductVariant).filter(
        ProductVariant.productVariantId == variant_id,
        (ProductVariant.deletedInd == False) | (ProductVariant.deletedInd.is_(None)),
    ).first()
    if not variant:
        raise HTTPException(status_code=404, detail="Variant not found")

    # Strict per-variant query — no product-level fallback.
    # The fallback (fetching all productId images when variant has none) was the
    # root cause of every variant showing the same image pool.
    images = db.query(ProductImage).filter(
        ProductImage.productVariantId == variant_id,
        (ProductImage.deletedInd == False) | (ProductImage.deletedInd.is_(None)),
    ).order_by(ProductImage.productImageId.asc()).all()

    return [
        {
            "productImageId":  img.productImageId,
            "productId":       img.productId,
            "productVariantId": img.productVariantId,
            "fileName":        img.fileName,
            "filePath":        resolve_url(img.filePath),
            "imageUrl":        resolve_url(img.filePath),
            "fileType":        img.fileType,
        }
        for img in images
    ]


# ─────────────────────────────────────────────────────────────
# UPLOAD IMAGES
# ─────────────────────────────────────────────────────────────

@router.post("/{variant_id}/images", status_code=201)
def upload_variant_images(
    variant_id: int,
    files: List[UploadFile] = File(...),
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

    # Use product name as the subfolder so each product's images are grouped together
    raw_folder_name = product.name or product.productCode or str(product.productId)
    product_folder = sanitize_folder_name(raw_folder_name)

    saved_images = []
    uploaded_blob_paths = []

    try:
        for f in files:
            if not f.content_type.startswith("image/"):
                raise HTTPException(status_code=400, detail="Only image files are allowed")

            # Validate size before uploading
            content = f.file.read()
            if len(content) > MAX_FILE_SIZE:
                raise HTTPException(status_code=400, detail="File too large (max 5MB)")
            f.file.seek(0)

            # upload_image handles container auto-creation + local fallback
            # Each product gets its own named subfolder: products/<ProductName>/
            file_url = upload_image(f, container_key="products", folder=product_folder)
            uploaded_blob_paths.append(file_url)

            img = ProductImage(
                productId        = variant.productId,
                productVariantId = variant_id,
                fileName         = f.filename,
                fileType         = f.content_type,
                filePath         = file_url,
            )
            db.add(img)
            db.flush()
            saved_images.append(img)

        db.commit()
    except HTTPException:
        db.rollback()
        for path in uploaded_blob_paths:
            delete_image(path)
        raise
    except Exception as exc:
        db.rollback()
        for path in uploaded_blob_paths:
            delete_image(path)
        raise HTTPException(
            status_code=500,
            detail=f"Image uploaded to storage but DB save failed: {exc}",
        ) from exc

    return {
        "uploaded": len(saved_images),
        "images": [
            {
                "productImageId": img.productImageId,
                "filePath":       resolve_url(img.filePath),
                "imageUrl":       resolve_url(img.filePath),
            }
            for img in saved_images
        ],
    }


# ─────────────────────────────────────────────────────────────
# DELETE IMAGE
# ─────────────────────────────────────────────────────────────

@router.delete("/{variant_id}/images/{image_id}", status_code=200)
def delete_variant_image(
    variant_id: int,
    image_id: int,
    db: Session = Depends(get_db)
):
    variant = db.query(ProductVariant).filter(
        ProductVariant.productVariantId == variant_id,
        (ProductVariant.deletedInd == False) | (ProductVariant.deletedInd.is_(None)),
    ).first()
    if not variant:
        raise HTTPException(status_code=404, detail="Variant not found")

    img = db.query(ProductImage).filter(
        ProductImage.productImageId == image_id,
        (ProductImage.deletedInd == False) | (ProductImage.deletedInd.is_(None)),
    ).first()

    if not img or (img.productVariantId not in (None, variant_id) and img.productId != variant.productId):
        raise HTTPException(status_code=404, detail="Image not found")

    # delete_image handles both Azure URLs and local /uploads/... paths
    if img.filePath:
        delete_image(img.filePath)

    img.deletedInd = True
    db.commit()

    return {"deleted": image_id}