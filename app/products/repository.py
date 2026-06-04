from datetime import datetime, timezone
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import asc, desc, text
from app.shared.filters import apply_filters, apply_ordering, apply_pagination, build_paged_response

from .models import Products


# ── Tax cascade helpers ───────────────────────────────────────────────────────

def recalculate_pvd_taxes_for_product(db: Session, product_id: int) -> dict:
    """
    Re-compute CGST / SGST / TaxAmount for every active PVD that belongs to
    this product.  Called whenever a product's TaxHSNCodeId is set or changed,
    and also exposed as a stand-alone admin endpoint for retroactive fixes.

    Returns a summary: {"updated": N, "skipped": M, "errors": [...]}.
    Skipped rows are those with no finalPrice (nothing to compute against).
    This function never raises — errors are collected and returned so the
    caller can surface them to the admin without rolling back the parent save.
    """
    try:
        from app.taxhsncode.repository import compute_tax_for_product
    except ImportError:
        return {"updated": 0, "skipped": 0, "errors": ["taxhsncode module not available"]}

    from app.productvariantdetail.models import ProductVariantDetail

    updated = skipped = 0
    errors: list = []

    try:
        pvds = db.query(ProductVariantDetail).filter(
            ProductVariantDetail.productId  == product_id,
            ProductVariantDetail.deletedInd == False,
        ).all()
    except Exception as exc:
        return {"updated": 0, "skipped": 0, "errors": [str(exc)]}

    for pvd in pvds:
        final_price = float(pvd.finalPrice or 0)
        if final_price <= 0:
            skipped += 1
            continue
        try:
            tax = compute_tax_for_product(db, product_id, final_price)
            pvd.cgst        = tax["cgstRate"]
            pvd.sgst        = tax["sgstRate"]
            pvd.taxAmount   = tax["taxAmount"]
            pvd.modifiedDate = datetime.now(timezone.utc)
            updated += 1
        except Exception as exc:
            errors.append(f"pvd {pvd.productVariantDetailId}: {exc}")
            skipped += 1

    if updated:
        try:
            db.commit()
        except Exception as exc:
            db.rollback()
            return {"updated": 0, "skipped": skipped, "errors": [f"commit failed: {exc}"]}

    return {"updated": updated, "skipped": skipped, "errors": errors}


from app.shared.url_helpers import resolve_url as _image_path_to_url  # noqa: F401


def _get_variant_images(db: Session, product_variant_id: int, product_id: int) -> list[dict]:
    # Strict per-variant query — no product-level fallback.
    # Falling back to productId would return images belonging to ALL variants
    # of the product, causing every colour swatch to show the same images.
    rows = db.execute(
        text(
            """
            SELECT "ProductImageId", "FileName", "FilePath", "FileType"
            FROM twam."ProductImage"
            WHERE "ProductVariantId" = :pvid
              AND ("DeletedInd" = false OR "DeletedInd" IS NULL)
              AND "FilePath" IS NOT NULL
            ORDER BY "ProductImageId" ASC
            """
        ),
        {"pvid": product_variant_id},
    ).fetchall()

    return [
        {
            "productImageId": r[0],
            "fileName":  r[1],
            # filePath is always the resolved full URL so every consumer
            # (ProductDetail, QuickShopModal, guest-cart storage, etc.) gets
            # the correct https:// URL without any extra client-side mapping.
            "filePath":  _image_path_to_url(r[2]),
            "fileType":  r[3],
            # fileData kept for backwards-compat; identical value.
            "fileData":  _image_path_to_url(r[2]),
        }
        for r in rows
    ]


def _get_product_variants(db: Session, product_id: int) -> list[dict]:
    try:
        rows = db.execute(
            text(
                """
            SELECT
                pv."ProductVariantId", pv."ProductId", pv."VariantName",
                pv."VariantDescription", pv."Color", pv."ColorFamilyId",
                pc."ColorFamilyName", pc."HexCode",
                pv."IsBestSeller", pv."IsReturnAvailable", pv."IsCupSize",
                pv."IsActive", pv."DeletedInd", pv."CreatedDate", pv."ModifiedDate"
            FROM twam."ProductVariants" pv
            LEFT JOIN mdm."ProductColor" pc
              ON pc."ProductColorId" = pv."ColorFamilyId"
             AND (pc."DeletedInd" = false OR pc."DeletedInd" IS NULL)
            WHERE pv."ProductId" = :pid
              AND (pv."DeletedInd" = false OR pv."DeletedInd" IS NULL)
            ORDER BY pv."ProductVariantId" ASC
            """
            ),
            {"pid": product_id},
        ).fetchall()
    except Exception:
        db.rollback()
        return []

    return [
        {
            "productVariantId": r[0],
            "productId": r[1],
            "variantName": r[2],
            "variantDescription": r[3],
            "color": r[4],
            "colorName": r[4],
            "colorFamilyId": r[5],
            "colorFamily": r[6],
            "colorFamilyHex": r[7],
            "colorHex": r[7],
            "isBestSeller": r[8],
            "isReturnAvailable": r[9],
            "isCupSize": r[10],
            "isActive": True if r[11] is None else bool(r[11]),
            "deletedInd": False if r[12] is None else bool(r[12]),
            "createdDate": r[13],
            "modifiedDate": r[14],
            "images": _get_variant_images(db, r[0], product_id),
        }
        for r in rows
    ]


def _to_response(db: Session, product: Products) -> dict:
    """Attach brandName + brand visibility flags, categoryName and imageUrl from joins."""
    brand_name       = None
    brand_is_active  = None
    brand_deleted    = None

    if product.brandId:
        try:
            row = db.execute(
                text(
                    'SELECT "brandName", "isActive", "deletedInd" '
                    'FROM mdm."Brand" WHERE "brandId" = :id LIMIT 1'
                ),
                {"id": product.brandId},
            ).fetchone()
            if row:
                brand_name      = row[0]
                brand_is_active = bool(row[1]) if row[1] is not None else True
                brand_deleted   = bool(row[2]) if row[2] is not None else False
        except Exception:
            db.rollback()

    # Resolve category name, active status, and deleted status.
    # NOTE: Do NOT filter by DeletedInd here — we need to detect deleted
    # categories so the admin UI can show "Inactive – Category deleted".
    category_name       = None
    category_is_active  = None
    category_deleted    = None
    cat_id = product.childCategoryId or product.categoryId
    if cat_id:
        try:
            row = db.execute(
                text(
                    'SELECT "Name", "IsActive", "DeletedInd" FROM twam."Category" '
                    'WHERE "CategoryId" = :id LIMIT 1'
                ),
                {"id": cat_id},
            ).fetchone()
            if row:
                category_name      = row[0]
                category_is_active = bool(row[1]) if row[1] is not None else True
                category_deleted   = bool(row[2]) if row[2] is not None else False
                # A deleted category always counts as inactive for product status
                if category_deleted:
                    category_is_active = False
        except Exception:
            db.rollback()

    # imageUrl is intentionally left null at the product level.
    # Each variant carries its own `images` list (from _get_variant_images),
    # so the frontend should always read images from the selected variant,
    # never from a product-level imageUrl which has no variant binding.
    image_url = None

    # Resolve Tax / HSN code details
    tax_hsn_code        = None
    tax_hsn_description = None
    total_gst           = None
    if product.taxHSNCodeId:
        try:
            hsn_row = db.execute(
                text("""
                    SELECT "HSNCode", "HSNDescription",
                           COALESCE("Slab1TotalGST", "Slab1CGST" + "Slab1SGST")
                    FROM mdm."TaxHSNCode"
                    WHERE "TaxHSNCodeId" = :id LIMIT 1
                """),
                {"id": product.taxHSNCodeId},
            ).fetchone()
            if hsn_row:
                tax_hsn_code        = hsn_row[0]
                tax_hsn_description = hsn_row[1]
                total_gst           = float(hsn_row[2]) if hsn_row[2] is not None else None
        except Exception:
            pass

    # Resolve Fabric name from mdm.Fabric
    fabric_name = None
    if product.fabricId:
        try:
            fab_row = db.execute(
                text('SELECT "FabricName" FROM mdm."Fabric" WHERE "FabricId" = :id LIMIT 1'),
                {"id": product.fabricId},
            ).fetchone()
            if fab_row:
                fabric_name = fab_row[0]
        except Exception:
            pass

    return {
        "productId":           product.productId,
        "productCode":         product.productCode,
        "name":                product.name,
        "description":         product.description,
        "categoryId":          product.categoryId,
        "childCategoryId":     product.childCategoryId,
        "categoryName":        category_name,
        "categoryIsActive":    category_is_active,
        "categoryDeletedInd":  category_deleted,
        "brandId":             product.brandId,
        "brandName":           brand_name,
        "brandIsActive":       brand_is_active,
        "brandDeletedInd":     brand_deleted,
        "fabricId":            product.fabricId,
        "fabricName":          fabric_name,
        "taxHSNCodeId":        product.taxHSNCodeId,
        "taxHSNCode":          tax_hsn_code,
        "taxHSNDescription":   tax_hsn_description,
        "totalGST":            total_gst,
        "personalId":          product.personalId,
        "userProfileId":       product.userProfileId,
        "tag":                 product.tag,
        "state":               product.state,
        "isActive":            product.isActive if product.isActive is not None else True,
        "deletedInd":          product.deletedInd if product.deletedInd is not None else False,
        "createdBy":           product.createdBy,
        "modifiedBy":          product.modifiedBy,
        "createdDate":         product.createdDate,
        "modifiedDate":        product.modifiedDate,
        "imageUrl":            image_url,
        "allVariants":         _get_product_variants(db, product.productId),
    }


def _active_brand_ids(db: Session) -> list[int]:
    """Return brandIds that are active AND not deleted — used for public filtering."""
    rows = db.execute(
        text(
            'SELECT "brandId" FROM mdm."Brand" '
            'WHERE "deletedInd" = false AND "isActive" = true'
        )
    ).fetchall()
    return [r[0] for r in rows]


def _active_category_ids(db: Session) -> list[int]:
    """Return categoryIds that are active AND not deleted — used for public filtering."""
    rows = db.execute(
        text(
            'SELECT "CategoryId" FROM twam."Category" '
            'WHERE "DeletedInd" = false AND "IsActive" = true'
        )
    ).fetchall()
    return [r[0] for r in rows]


def get_all_products(
    db: Session,
    filters: Optional[List[dict]] = None,
    order_ascending: Optional[bool] = None,
    order_property: Optional[str] = None,
    page_index: Optional[int] = None,
    page_size: Optional[int] = None,
    public_only: bool = True,   # False = admin view (shows all products regardless of brand/category status)
) -> dict:
    query = db.query(Products).filter(Products.deletedInd == False)

    # ── Visibility gates (public storefront only) ──────────────────────────────
    # Hide products that are inactive (isActive=False) or whose brand/category
    # has been deactivated / deleted.
    # Null brand / null category are always shown (no gate to block them).
    if public_only:
        from sqlalchemy import or_

        # Own product visibility gate
        query = query.filter(Products.isActive == True)

        # Brand gate: exclude products whose brand is hidden or deleted
        active_brand_ids = _active_brand_ids(db)
        query = query.filter(
            or_(
                Products.brandId == None,                    # no brand → always show
                Products.brandId.in_(active_brand_ids),      # brand is active & not deleted
            )
        )

        # Category gate: exclude products whose category is inactive or deleted
        active_cat_ids = _active_category_ids(db)
        query = query.filter(
            or_(
                Products.childCategoryId == None,            # prefer child category …
                Products.childCategoryId.in_(active_cat_ids),
            )
        ).filter(
            or_(
                Products.categoryId == None,
                Products.categoryId.in_(active_cat_ids),     # … fallback to parent category
            )
        )

    query = apply_filters(query, Products, filters)
    query = apply_ordering(query, Products, order_property, order_ascending)

    total = query.count()

    if page_index and page_size:
        query = query.offset((page_index - 1) * page_size).limit(page_size)

    rows = query.all()
    return {
        "count":      total,
        "list":       [_to_response(db, r) for r in rows],
        "parameters": None,
    }


def get_product_by_id(db: Session, product_id: int) -> Optional[dict]:
    product = db.query(Products).filter(
        Products.productId == product_id,
        Products.deletedInd == False,
    ).first()
    if not product:
        return None
    return _to_response(db, product)


def create_product(db: Session, data) -> int:
    product = Products(
        productCode     = data.productCode,
        name            = data.name,
        description     = data.description,
        categoryId      = data.categoryId,
        childCategoryId = data.childCategoryId,
        brandId         = data.brandId,
        fabricId        = data.fabricId,
        taxHSNCodeId    = getattr(data, "taxHSNCodeId", None),
        personalId      = data.personalId,
        userProfileId   = data.userProfileId,
        state           = data.state,
        tag             = data.tag,
        createdBy       = data.createdBy,
        createdDate     = datetime.now(timezone.utc),
        isActive        = True,
        deletedInd      = False,
    )
    db.add(product)
    db.commit()
    db.refresh(product)

    try:
        db.execute(
            text("SELECT twam.\"AutoCreateProductAudit\"(:ref_id, :profile)"),
            {"ref_id": product.productId, "profile": "Product"}
        )
        db.commit()
    except Exception:
        pass

    return product.productId


def update_product(db: Session, product_id: int, data) -> Optional[int]:
    product = db.query(Products).filter(
        Products.productId == product_id,
        Products.deletedInd == False,
    ).first()
    if not product:
        return None

    # Track whether TaxHSNCodeId is changing so we can cascade to PVDs below.
    prev_hsn_id = product.taxHSNCodeId
    new_hsn_id  = getattr(data, "taxHSNCodeId", None)
    hsn_changed = (new_hsn_id is not None) and (new_hsn_id != prev_hsn_id)

    for field in ("productCode", "name", "description", "categoryId",
                  "brandId", "taxHSNCodeId", "personalId",
                  "userProfileId", "state", "tag", "modifiedBy"):
        val = getattr(data, field, None)
        if val is not None:
            setattr(product, field, val)

    # childCategoryId is special: it can be explicitly set to null (to clear
    # the child link when parent category changes). Use model_fields_set to
    # distinguish "explicitly passed null" from "field was not sent at all".
    if "childCategoryId" in data.model_fields_set:
        product.childCategoryId = data.childCategoryId  # accepts None to clear

    if data.fabricId is not None:
        product.fabricId = data.fabricId

    product.modifiedDate = datetime.now(timezone.utc)
    db.commit()

    try:
        db.execute(
            text("SELECT twam.\"AutoCreateProductAudit\"(:ref_id, :profile)"),
            {"ref_id": product_id, "profile": "Product"}
        )
        db.commit()
    except Exception:
        # The stored procedure failed (missing, or PL/pgSQL error).
        # PostgreSQL marks the whole connection transaction as aborted —
        # any subsequent query on this session raises InFailedSqlTransaction.
        # Roll back so the rest of the function can continue safely.
        # The product update itself was already committed before this block.
        db.rollback()

    # ── Cascade: recalculate CGST/SGST on all PVDs when HSN is set/changed ──
    # Covers both first-time assignment (prev was None) and HSN reassignment.
    # Also covers re-saving the same HSN so existing PVDs get corrected.
    # The product save is already committed; PVD tax errors are non-fatal.
    #
    # Use prev_hsn_id captured before commit — do NOT lazy-load product.taxHSNCodeId
    # here because after a rollback() the ORM object is expired and re-fetching
    # on a fresh transaction could still race. Pre-captured values are safe.
    effective_hsn = new_hsn_id or prev_hsn_id
    if hsn_changed or (new_hsn_id and not prev_hsn_id) or effective_hsn:
        try:
            recalculate_pvd_taxes_for_product(db, product_id)
        except Exception:
            pass  # never block the product update

    return product_id


def toggle_product_active(db: Session, product_id: int) -> Optional[dict]:
    """Toggle isActive (hide/show) for a product. This is separate from delete."""
    product = db.query(Products).filter(
        Products.productId == product_id,
        Products.deletedInd == False,
    ).first()
    if not product:
        return None

    new_state = not bool(product.isActive)
    product.isActive     = new_state
    product.modifiedDate = datetime.now(timezone.utc)
    db.commit()

    # Cascade to variants and variant details
    try:
        db.execute(
            text('UPDATE twam."ProductVariants" SET "IsActive" = :active WHERE "ProductId" = :pid AND "DeletedInd" = false'),
            {"active": new_state, "pid": product_id}
        )
        db.execute(
            text('UPDATE twam."ProductVariantDetail" SET "IsActive" = :active WHERE "ProductId" = :pid AND "DeletedInd" = false'),
            {"active": new_state, "pid": product_id}
        )
        db.commit()
    except Exception:
        pass

    return {"productId": product_id, "isActive": new_state}


def deactivate_products_by_brand(db: Session, brand_id: int) -> int:
    """Set isActive=False on all non-deleted products of a brand (used when brand is hidden/deleted)."""
    try:
        result = db.execute(
            text(
                'UPDATE twam."Products" SET "IsActive" = false '
                'WHERE "BrandId" = :bid AND "DeletedInd" = false AND "IsActive" = true'
            ),
            {"bid": brand_id},
        )
        db.commit()
        return result.rowcount or 0
    except Exception:
        return 0


def reactivate_products_by_brand(db: Session, brand_id: int) -> int:
    """Set isActive=True on all non-deleted products of a brand (used when brand is re-shown)."""
    try:
        result = db.execute(
            text(
                'UPDATE twam."Products" SET "IsActive" = true '
                'WHERE "BrandId" = :bid AND "DeletedInd" = false AND "IsActive" = false'
            ),
            {"bid": brand_id},
        )
        db.commit()
        return result.rowcount or 0
    except Exception:
        return 0


def deactivate_products_by_category(db: Session, category_id: int) -> int:
    """Set isActive=False on all non-deleted products of a category (used when category is hidden/deleted)."""
    try:
        result = db.execute(
            text(
                'UPDATE twam."Products" SET "IsActive" = false '
                'WHERE ("CategoryId" = :cid OR "ChildCategoryId" = :cid) '
                'AND "DeletedInd" = false AND "IsActive" = true'
            ),
            {"cid": category_id},
        )
        db.commit()
        return result.rowcount or 0
    except Exception:
        return 0


def reactivate_products_by_category(db: Session, category_id: int) -> int:
    """Set isActive=True on all non-deleted products of a category (used when category is re-shown)."""
    try:
        result = db.execute(
            text(
                'UPDATE twam."Products" SET "IsActive" = true '
                'WHERE ("CategoryId" = :cid OR "ChildCategoryId" = :cid) '
                'AND "DeletedInd" = false AND "IsActive" = false'
            ),
            {"cid": category_id},
        )
        db.commit()
        return result.rowcount or 0
    except Exception:
        return 0


def delete_product(db: Session, product_id: int) -> bool:
    product = db.query(Products).filter(
        Products.productId == product_id,
        Products.deletedInd == False,
    ).first()
    if not product:
        return False

    product.deletedInd   = True
    product.isActive     = False
    product.modifiedDate = datetime.now(timezone.utc)
    db.commit()

    try:
        db.execute(text(
            'UPDATE twam."ProductVariants" SET "DeletedInd" = true, "IsActive" = false WHERE "ProductId" = :pid'
        ), {"pid": product_id})
        db.execute(text(
            'UPDATE twam."ProductVariantDetail" SET "DeletedInd" = true, "IsActive" = false WHERE "ProductId" = :pid'
        ), {"pid": product_id})
        db.execute(text(
            'UPDATE twam."ProductImage" SET "DeletedInd" = true WHERE "ProductId" = :pid'
        ), {"pid": product_id})
        db.commit()
    except Exception:
        pass

    return True


def search_products_by_name_or_code(
    db: Session,
    q: str,
    page_index: int = 1,
    page_size: int  = 20,
) -> dict:
    """
    Case-insensitive search across Name OR ProductCode.
    Returns the same ProductListResponse envelope as get_all_products.
    """
    term = f"%{q}%"
    base = (
        db.query(Products)
        .filter(
            Products.deletedInd == False,
            (Products.name.ilike(term)) | (Products.productCode.ilike(term)),
        )
        .order_by(Products.name)
    )
    total = base.count()
    rows  = base.offset((page_index - 1) * page_size).limit(page_size).all()
    return {
        "count": total,
        "list":  [_to_response(db, p) for p in rows],
        "parameters": None,
    }


def search_products_by_stock_id(db: Session, stock_id: str) -> dict:
    """
    Find the product(s) that own a ProductVariantDetail whose StockId matches
    the given value (exact, case-insensitive).
    Returns the same ProductListResponse envelope.
    """
    rows = db.execute(
        text("""
            SELECT DISTINCT p."ProductId"
            FROM twam."Products" p
            JOIN twam."ProductVariantDetail" pvd
              ON pvd."ProductId" = p."ProductId"
             AND pvd."DeletedInd" = false
            WHERE p."DeletedInd" = false
              AND LOWER(pvd."StockId") = LOWER(:sid)
        """),
        {"sid": stock_id},
    ).fetchall()

    product_ids = [r[0] for r in rows]
    products    = []
    for pid in product_ids:
        p = db.query(Products).filter(Products.productId == pid).first()
        if p:
            products.append(_to_response(db, p))

    return {
        "count": len(products),
        "list":  products,
        "parameters": None,
    }


def check_product_validations(db: Session, product_id: int, state: Optional[str] = None) -> bool:
    if state == "Returned For Correction":
        count = db.execute(
            text("""
                SELECT COUNT(*) FROM twam."ProductVariantDetail"
                WHERE "DeletedInd" = false
                  AND "ProductId" = :pid
                  AND "ProcessedQuantity" > 0
            """),
            {"pid": product_id},
        ).scalar()
        return (count or 0) > 0

    variant_count = db.execute(
        text('SELECT COUNT(*) FROM twam."ProductVariants" WHERE "DeletedInd" = false AND "ProductId" = :pid'),
        {"pid": product_id},
    ).scalar()
    detail_count = db.execute(
        text('SELECT COUNT(*) FROM twam."ProductVariantDetail" WHERE "DeletedInd" = false AND "ProductId" = :pid'),
        {"pid": product_id},
    ).scalar()
    return (variant_count or 0) > 0 and (detail_count or 0) > 0