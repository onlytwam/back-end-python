import base64
import json
from datetime import datetime, timezone
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import asc, desc, text
from app.productvariantdetail.models import ProductVariantDetail
from app.shared.filters import apply_filters, apply_ordering, apply_pagination, build_paged_response
from app.shared.url_helpers import resolve_url as _resolve_url, storage_path_for_db

from .models import ProductVariant, ProductImage
    

# def _get_images(db: Session, product_variant_id: int) -> list:
#     rows = db.execute(
#         text("""
#             SELECT "ProductImageId", "FileName", "FilePath", "FileType"
#             FROM twam."ProductImage"
#             WHERE "ProductVariantId" = :pvid
#               AND ("DeletedInd" = false OR "DeletedInd" IS NULL)
#         """),
#         {"pvid": product_variant_id}
#     ).fetchall()
#     return [
#         {"productImageId": r[0], "fileName": r[1], "filePath": r[2], "fileType": r[3]}
#         for r in rows
#     ]


def _get_images(db: Session, product_variant_id: int, product_id: int) -> list:
    # Strict per-variant query — no product-level fallback.
    # Falling back to productId returns images belonging to ALL variants of the
    # product, causing every colour swatch to show the same image pool.
    rows = db.execute(
        text("""
            SELECT "ProductImageId", "FileName", "FilePath", "FileType"
            FROM twam."ProductImage"
            WHERE "ProductVariantId" = :pvid
              AND ("DeletedInd" = false OR "DeletedInd" IS NULL)
              AND "FilePath" IS NOT NULL
        """),
        {"pvid": product_variant_id}
    ).fetchall()

    return [
        {
            "productImageId": r[0],
            "fileName": r[1],
            "filePath": _resolve_url(r[2]),
            "fileType": r[3],
        }
        for r in rows
    ]


def _get_color_family(db: Session, color_family_id: Optional[int]) -> dict:
    if not color_family_id:
        return {"name": None, "hex": None}
    try:
        row = db.execute(text("""
            SELECT "ColorFamilyName", "HexCode"
            FROM mdm."ProductColor"
            WHERE "ProductColorId" = :id
              AND ("DeletedInd" = false OR "DeletedInd" IS NULL)
            LIMIT 1
        """), {"id": color_family_id}).fetchone()
        if row:
            return {"name": row[0], "hex": row[1]}
    except Exception:
        db.rollback()
    return {"name": None, "hex": None}


def _to_response(db: Session, variant: ProductVariant) -> dict:
    family = _get_color_family(db, variant.colorFamilyId)
    return {
        "productVariantId":   variant.productVariantId,
        "productId":          variant.productId,
        "variantName":        variant.variantName,
        "variantDescription": variant.variantDescription,
        "userProfileId":      variant.userProfileId,
        "state":              variant.state,
        # productCode, fabricId, taxHSNCodeId removed from variant level
        "color":              variant.color,
        "colorFamilyId":      variant.colorFamilyId,
        "colorFamily":        family["name"],
        "colorFamilyHex":     family["hex"],
        "colorHex":           family["hex"],
        "isBestSeller":       variant.isBestSeller,
        "isReturnAvailable":  variant.isReturnAvailable,
        "isCupSize":          variant.isCupSize,
        "isAlphaSize":        variant.isAlphaSize,
        "createdDate":        variant.createdDate,
        "modifiedDate":       variant.modifiedDate,
        "images":             _get_images(db, variant.productVariantId, variant.productId),
        "deletedInd":         variant.deletedInd,
        "isActive":           variant.isActive if variant.isActive is not None else True,
    }


# ── Grid / List ───────────────────────────────────────────────────────────────

def get_all_variants(
    db: Session,
    filters=None,
    order_ascending: bool = True,
    order_property: str = None,
    page_index: int = 1,
    page_size: int = 50,
):
    from sqlalchemy import or_
    query = db.query(ProductVariant).filter(
        # Treat NULL deletedInd the same as False (many rows were inserted before the column existed)
        or_(ProductVariant.deletedInd == False, ProductVariant.deletedInd == None)  # noqa: E711, E712
    )

    # ✅ APPLY FILTERS
    if filters:
        for f in filters:
            prop = f.get("property")
            value = f.get("value")

            if prop in ["ProductId", "productId"]:
                query = query.filter(ProductVariant.productId == int(value))

            elif prop in ["ProductVariantId", "productVariantId"]:
                query = query.filter(ProductVariant.productVariantId == int(value))

    #  ORDERING
    if order_property:
        column = getattr(ProductVariant, order_property, None)
        if column is not None:
            query = query.order_by(asc(column) if order_ascending else desc(column))

    #  COUNT before pagination
    total = query.count()

    #  PAGINATION
    if page_index and page_size:
        query = query.offset((page_index - 1) * page_size).limit(page_size)

    results = query.all()

    # Return full dicts (with images) so the frontend receives images[] on every variant
    return {
        "list": [_to_response(db, v) for v in results],
        "count": total,
    }
# ── Single by ID ──────────────────────────────────────────────────────────────

def get_variant_by_id(db: Session, variant_id: int) -> Optional[dict]:
    from sqlalchemy import or_
    variant = db.query(ProductVariant).filter(
        ProductVariant.productVariantId == variant_id,
        or_(ProductVariant.deletedInd == False, ProductVariant.deletedInd == None)  # noqa: E711, E712
    ).first()
    if not variant:
        return None
    result = _to_response(db, variant)
    for img in result["images"]:
        if img.get("filePath"):
            fp = img.get("filePath") or ""
            img["fileData"] = _resolve_url(fp)
    return result


# ── Create ────────────────────────────────────────────────────────────────────

def create_variant(db: Session, data, images: Optional[list] = None) -> int:
    variant = ProductVariant(
        productId          = data.productId,
        variantName        = data.variantName,
        variantDescription = data.variantDescription,
        userProfileId      = data.userProfileId,
        state              = data.state,
        # productCode removed — lives on Products
        # fabricId    removed — lives on Products
        color              = data.color,
        colorFamilyId      = data.colorFamilyId,
        isBestSeller       = data.isBestSeller,
        isReturnAvailable  = data.isReturnAvailable,
        isCupSize          = data.isCupSize,
        isAlphaSize        = data.isAlphaSize,
        createdBy          = data.createdBy,
        createdDate        = datetime.now(timezone.utc),
        deletedInd         = False,
    )
    db.add(variant)
    db.flush()
    variant_id = variant.productVariantId

    if images:
        for img in images:
            db.add(ProductImage(
                productId        = data.productId,
                productVariantId = variant_id,
                fileName         = img.get("fileName"),
                fileType         = img.get("fileType"),
                filePath         = storage_path_for_db(img.get("filePath")),
                createdDate      = datetime.now(timezone.utc),
                deletedInd       = False,
            ))

    db.commit()

    try:
        db.execute(
            text("SELECT twam.\"AutoCreateProductAudit\"(:ref_id, :profile)"),
            {"ref_id": variant_id, "profile": "ProductVariant"}
        )
        db.commit()
    except Exception:
        pass

    return variant_id


# ── Update ────────────────────────────────────────────────────────────────────

def update_variant(db: Session, variant_id: int, data, images: Optional[list] = None) -> Optional[int]:
    variant = db.query(ProductVariant).filter(
        ProductVariant.productVariantId == variant_id
    ).first()
    if not variant:
        return None

    variant.variantName        = data.variantName        if data.variantName        is not None else variant.variantName
    variant.variantDescription = data.variantDescription if data.variantDescription is not None else variant.variantDescription
    # productCode, fabricId, taxHSNCodeId removed from variant — live on Products
    variant.color              = data.color              if data.color              is not None else variant.color
    if "colorFamilyId" in data.model_fields_set:
        variant.colorFamilyId = data.colorFamilyId
    variant.isBestSeller       = data.isBestSeller       if data.isBestSeller       is not None else variant.isBestSeller
    variant.isReturnAvailable  = data.isReturnAvailable  if data.isReturnAvailable  is not None else variant.isReturnAvailable
    variant.isCupSize          = data.isCupSize          if data.isCupSize          is not None else variant.isCupSize
    if "isAlphaSize" in data.model_fields_set:
        variant.isAlphaSize = data.isAlphaSize
    variant.modifiedBy         = data.modifiedBy         if data.modifiedBy         is not None else variant.modifiedBy
    variant.modifiedDate       = datetime.now(timezone.utc)

    if images:
        db.execute(
            text('UPDATE twam."ProductImage" SET "DeletedInd"=true WHERE "ProductVariantId"=:pvid AND ("DeletedInd"=false OR "DeletedInd" IS NULL)'),
            {"pvid": variant_id}
        )
        db.flush()
        for img in images:
            db.add(ProductImage(
                productId        = variant.productId,
                productVariantId = variant_id,
                fileName         = img.get("fileName"),
                fileType         = img.get("fileType"),
                filePath         = storage_path_for_db(img.get("filePath")),
                createdDate      = datetime.now(timezone.utc),
                deletedInd       = False,
            ))

    db.commit()

    try:
        db.execute(
            text("SELECT twam.\"AutoCreateProductAudit\"(:ref_id, :profile)"),
            {"ref_id": variant_id, "profile": "ProductVariant"}
        )
        db.commit()
    except Exception:
        pass

    return variant_id


# ── Delete ────────────────────────────────────────────────────────────────────

def delete_variant(db: Session, variant_id: int):
    try:
        # 1. Delete Variant Details
        db.query(ProductVariantDetail).filter(
            ProductVariantDetail.productVariantId == variant_id
        ).delete(synchronize_session=False)

        # 2. Delete Images
        db.query(ProductImage).filter(
            ProductImage.productVariantId == variant_id
        ).delete(synchronize_session=False)

        # 3. Delete Variant
        variant = db.query(ProductVariant).filter(
            ProductVariant.productVariantId == variant_id
        ).first()

        if not variant:
            return False

        db.delete(variant)
        db.commit()

        return True

    except Exception as e:
        db.rollback()
        raise e


# ── User-facing: Product Detail Page ─────────────────────────────────────────

def get_user_product_variant(db: Session, product_variant_id: int = 0, name: str = "") -> Optional[dict]:
    if product_variant_id > 0:
        where  = 'pv."ProductVariantId" = :pvid'
        params = {"pvid": product_variant_id}
    elif name:
        where  = '(pv."VariantName" ILIKE :name OR p."Name" ILIKE :name)'
        params = {"name": f"%{name}%"}
    else:
        return None

    # Size type and cup-size presence are detected from the product's own
    # ProductVariantDetail rows — NOT from category settings.
    sql = f"""
        SELECT
            pv."ProductVariantId", pv."ProductId",
            p."Name", p."Description", p."ProductCode", p."Tag", p."CategoryId",
            pv."IsCupSize", pv."IsAlphaSize", pv."IsReturnAvailable",
            b."brandName",
            p."ChildCategoryId",
            COALESCE(cc."IsActive", pc."IsActive", true) AS categoryIsActive
        FROM twam."ProductVariants" pv
        LEFT JOIN twam."Products" p ON p."ProductId" = pv."ProductId"
        LEFT JOIN mdm."Brand" b ON b."brandId" = p."BrandId"
        LEFT JOIN twam."Category" pc ON pc."CategoryId"  = p."CategoryId"
        LEFT JOIN twam."Category" cc ON cc."CategoryId"  = p."ChildCategoryId"
        WHERE (pv."DeletedInd" = false OR pv."DeletedInd" IS NULL)
          AND (pv."IsActive" = true OR pv."IsActive" IS NULL)
          AND {where}
        LIMIT 1
    """
    row = db.execute(text(sql), params).fetchone()
    if not row:
        return None

    pv_id     = row[0]
    prod_id   = row[1]
    prod_name = row[2] or ""

    is_cup_size  = bool(row[7])
    is_alpha_size = bool(row[8])   # IsAlphaSize — drives which mdm.Size SizeType to load

    # Strict per-variant query — no product-level fallback.
    img_rows = db.execute(text(
        'SELECT "FilePath" FROM twam."ProductImage" WHERE "ProductVariantId"=:pvid AND ("DeletedInd"=false OR "DeletedInd" IS NULL) AND "FilePath" IS NOT NULL'
    ), {"pvid": pv_id}).fetchall()

    images = [
        _resolve_url(r[0])
        for r in img_rows if r[0]
    ]

    pvd_row = db.execute(text("""
        SELECT "FinalPrice","MRPPrice","AvailableQuantity","DiscountPercent","Size"
        FROM twam."ProductVariantDetail"
        WHERE "ProductVariantId" = :pvid
          AND ("DeletedInd" = false OR "DeletedInd" IS NULL)
        ORDER BY
          CASE WHEN COALESCE("AvailableQuantity", "StockQuantity", 0) > 0 THEN 0 ELSE 1 END,
          COALESCE("FinalPrice", "MRPPrice") ASC NULLS LAST,
          "ProductVariantDetailId" ASC
        LIMIT 1
    """), {"pvid": pv_id}).fetchone()

    rating_row = db.execute(text("""
        SELECT
            ROUND(AVG(pr."Rating"::NUMERIC), 1) AS avg_rating,
            COUNT(pr."ProductReviewId")         AS review_count
        FROM twam."ProductReview" pr
        WHERE pr."ProductVariantId" = :pvid
          AND (pr."DeletedInd" = false OR pr."DeletedInd" IS NULL)
    """), {"pvid": pv_id}).fetchone()

    avg_rating   = float(rating_row[0]) if rating_row and rating_row[0] is not None else None
    review_count = int(rating_row[1])   if rating_row and rating_row[1] else 0

    try:
        pvd_matrix_rows = db.execute(text("""
            SELECT pvd."Size", pvd."CupSize", pvd."ProductVariantDetailId",
                   COALESCE(pvd."AvailableQuantity", pvd."StockQuantity", 0) AS stock,
                   pvd."FinalPrice", pvd."MRPPrice"
            FROM twam."ProductVariantDetail" pvd
            WHERE pvd."ProductVariantId" = :pvid
              AND (pvd."DeletedInd" = false OR pvd."DeletedInd" IS NULL)
            ORDER BY pvd."Size" NULLS LAST, pvd."CupSize" NULLS LAST
        """), {"pvid": pv_id}).fetchall()
    except Exception:
        pvd_matrix_rows = []

    pvd_matrix = [{
        "sizeId": r[0], "cupSizeId": r[1], "productVariantDetailId": r[2],
        "stock":       int(r[3])   if r[3] is not None else 0,
        "finalPrice":  float(r[4]) if r[4] is not None else None,
        "mrpPrice":    float(r[5]) if r[5] is not None else None,
    } for r in pvd_matrix_rows]

    variant_size_ids = {r[0] for r in pvd_matrix_rows if r[0]}
    variant_cup_ids  = {r[1] for r in pvd_matrix_rows if r[1]}
    size_stocks: dict = {}
    for r in pvd_matrix_rows:
        if r[0]: size_stocks[r[0]] = size_stocks.get(r[0], 0) + (int(r[3]) if r[3] else 0)
    pvd_by_size: dict = {}
    for r in pvd_matrix_rows:
        if r[0] and r[0] not in pvd_by_size: pvd_by_size[r[0]] = r[2]

    # ── Auto-detect cup-size presence from actual PVD data ───────────────────
    # If ANY PVD row has a non-null CupSize FK, treat this as a cup-size product
    # regardless of the IsCupSize flag on the variant (which may be NULL/False
    # if set via bulk import or not updated after cup-size PVDs were added).
    if variant_cup_ids:
        is_cup_size = True

    # ── Auto-detect alpha size from actual PVD size IDs ──────────────────────
    # If IsAlphaSize flag is False/NULL but the sizes linked in PVDs belong to
    # the 'alphabetical' SizeType in mdm.Size, override so labels render correctly.
    if not is_alpha_size and variant_size_ids:
        try:
            alpha_check = db.execute(text("""
                SELECT COUNT(*) FROM mdm."Size"
                WHERE "SizeId" = ANY(:ids)
                  AND "SizeType" = 'alphabetical'
                  AND ("DeletedInd" = false OR "DeletedInd" IS NULL)
            """), {"ids": list(variant_size_ids)}).scalar()
            if alpha_check and alpha_check > 0:
                is_alpha_size = True
        except Exception:
            pass

    # ── Load sizes: driven by variant.isAlphaSize flag ──────────────────────
    # True  → alphabetical (XS/S/M/L/XL…); False/None → numerical (28/30/32…)
    _stype = "alphabetical" if is_alpha_size else "numerical"
    all_sizes_rows = db.execute(text(f"""
        SELECT "SizeId", "SizeLabel", "SizeCode" FROM mdm."Size"
        WHERE ("DeletedInd" = false OR "DeletedInd" IS NULL)
          AND ("IsActive" = true OR "IsActive" IS NULL)
          AND ("IsCupSize" IS NULL OR "IsCupSize" = false)
          AND "SizeType" = '{_stype}'
        ORDER BY "OrderNo" NULLS LAST, "SizeId"
    """)).fetchall()

    # ── Cup sizes: driven by is_cup_size (flag or auto-detected from PVD data) ─
    all_cup_rows = []
    if is_cup_size:
        all_cup_rows = db.execute(text("""
            SELECT "CupSizeId", "SizeLabel" FROM mdm."CupSize"
            WHERE ("DeletedInd" = false OR "DeletedInd" IS NULL)
              AND ("IsActive" = true OR "IsActive" IS NULL)
            ORDER BY "CupSizeId"
        """)).fetchall()

    color_rows = db.execute(text("""
        SELECT pv2."ProductVariantId", pv2."Color", pv2."ColorFamilyId",
            pc."ColorFamilyName", pc."HexCode",
            (SELECT ARRAY_AGG(pi2."FilePath" ORDER BY pi2."ProductImageId")
             FROM twam."ProductImage" pi2
             WHERE pi2."ProductVariantId" = pv2."ProductVariantId"
               AND (pi2."DeletedInd" = false OR pi2."DeletedInd" IS NULL)
               AND pi2."FilePath" IS NOT NULL) AS images_arr,
            best_pvd."FinalPrice" AS min_final_price,
            best_pvd."MRPPrice" AS min_mrp_price,
            best_pvd."ProductVariantDetailId" AS min_pvd_id,
            best_pvd."Size" AS min_size_id,
            best_pvd."CupSize" AS min_cup_size_id,
            COALESCE(best_pvd."AvailableQuantity", best_pvd."StockQuantity", 0) AS min_stock
        FROM twam."ProductVariants" pv2
        LEFT JOIN mdm."ProductColor" pc
          ON pc."ProductColorId" = pv2."ColorFamilyId"
         AND (pc."DeletedInd" = false OR pc."DeletedInd" IS NULL)
        LEFT JOIN LATERAL (
            SELECT pvd2."ProductVariantDetailId", pvd2."FinalPrice", pvd2."MRPPrice",
                   pvd2."Size", pvd2."CupSize", pvd2."AvailableQuantity", pvd2."StockQuantity"
            FROM twam."ProductVariantDetail" pvd2
            WHERE pvd2."ProductVariantId" = pv2."ProductVariantId"
              AND (pvd2."DeletedInd" = false OR pvd2."DeletedInd" IS NULL)
            ORDER BY
              CASE WHEN COALESCE(pvd2."AvailableQuantity", pvd2."StockQuantity", 0) > 0 THEN 0 ELSE 1 END,
              COALESCE(pvd2."FinalPrice", pvd2."MRPPrice") ASC NULLS LAST,
              pvd2."ProductVariantDetailId" ASC
            LIMIT 1
        ) best_pvd ON true
        WHERE pv2."ProductId" = :pid
          AND (pv2."DeletedInd" = false OR pv2."DeletedInd" IS NULL)
          AND (pv2."IsActive" = true OR pv2."IsActive" IS NULL)
        ORDER BY
          COALESCE(best_pvd."FinalPrice", best_pvd."MRPPrice") ASC NULLS LAST,
          pv2."ProductVariantId" ASC
    """), {"pid": prod_id}).fetchall()

    return {
        "productId":          prod_id,
        "productVariantId":   pv_id,
        "name":               prod_name,
        "description":        row[3],
        "productCode":        row[4],
        "tag":                row[5],
        "categoryId":         row[6],
        "isCupSize":          is_cup_size,
        "hasCupSize":         is_cup_size,   # alias — frontend reads hasCupSize
        "isAlphaSize":        is_alpha_size,
        "isReturnAvailable":  row[9],
        "categoryIsActive":   bool(row[12]) if row[12] is not None else True,
        "brandName":          row[10],
        "price":              float(pvd_row[0]) if pvd_row and pvd_row[0] is not None else None,
        "originalPrice":      float(pvd_row[1]) if pvd_row and pvd_row[1] is not None else None,
        "stockQuantity":      pvd_row[2]         if pvd_row else None,
        "discountPercentage": float(pvd_row[3]) if pvd_row and pvd_row[3] is not None else None,
        "images":             images,
        "rating":             avg_rating,
        "reviewCount":        review_count,
        "pvdMatrix":          pvd_matrix,
        "sizes": [{
            "sizeId":                 s[0],
            "sizeLabel":              s[1],
            "sizeCode":               s[2],
            "inVariant":              s[0] in variant_size_ids,
            "hasStock":               size_stocks.get(s[0], 0) > 0,
            "stock":                  size_stocks.get(s[0], 0),
            "productVariantDetailId": pvd_by_size.get(s[0]),
        } for s in all_sizes_rows],
        "cupSizes": [{
            "cupSizeId": c[0], "sizeLabel": c[1],
            "inVariant": c[0] in variant_cup_ids,
            "hasStock":  any(r[1] == c[0] and (r[3] or 0) > 0 for r in pvd_matrix_rows),
        } for c in all_cup_rows],
        "colors": [{
            "productVariantId": cr[0],
            "color":      cr[1],
            "colorName":  cr[1],   # alias — frontend reads colorName first
            "colorFamilyId": cr[2],
            "colorFamily": cr[3],
            "colorFamilyHex": cr[4],
            "colorHex": cr[4],
            # images: full per-variant list used by ProductDetail gallery
            "images": [
                {"filePath": _resolve_url(p), "fileData": _resolve_url(p)}
                for p in (cr[5] or []) if p
            ],
            # image: first image, kept for swatch thumbnail backward-compat
            "image": _resolve_url(cr[5][0]) if cr[5] else None,
            "minFinalPrice": float(cr[6]) if cr[6] is not None else None,
            "minMrpPrice": float(cr[7]) if cr[7] is not None else None,
            "minProductVariantDetailId": cr[8],
            "minSizeId": cr[9],
            "minCupSizeId": cr[10],
            "minStock": int(cr[11]) if cr[11] is not None else 0,
        } for cr in color_rows],
    }


# ── User-facing: Product List ─────────────────────────────────────────────────

def get_user_product_list(db: Session, filters: list, page_index: int, page_size: int,
                          order_property: str, order_ascending: bool) -> dict:
    return _fallback_product_list(db, filters, page_index, page_size, order_property, order_ascending)


# ─── Core product list SQL ────────────────────────────────────────────────────

def _get_descendant_category_ids(db, root_id: str) -> list:
    try:
        root = int(root_id)
    except (ValueError, TypeError):
        return []
    rows = db.execute(
        text('SELECT "CategoryId", "ParentCategoryId" FROM twam."Category" WHERE ("DeletedInd"=false OR "DeletedInd" IS NULL) AND "IsActive"=true')
    ).fetchall()
    children_map: dict = {}
    for cat_id, parent_id in rows:
        if parent_id not in children_map:
            children_map[parent_id] = []
        children_map[parent_id].append(cat_id)
    result = []
    queue  = [root]
    while queue:
        cur = queue.pop(0)
        result.append(cur)
        for child in children_map.get(cur, []):
            if child not in result:
                queue.append(child)
    return result


def _fallback_product_list(db, filters, page_index, page_size, order_property, order_ascending):
    def _fv(prop, default=""):
        for f in (filters or []):
            if f.get("property") == prop:
                return f.get("value", default) or default
        return default

    conds = [
        "(pv.\"DeletedInd\" = false OR pv.\"DeletedInd\" IS NULL)",
        "(p.\"DeletedInd\"  = false OR p.\"DeletedInd\"  IS NULL)",
        # Only show the currently selected variant if it is active.
        # If a specific variant is hidden, skip it — but the product still
        # appears in the listing if it has OTHER active variants (handled via
        # the DISTINCT ON logic: only active variants drive the listing rows).
        "(pv.\"IsActive\" = true OR pv.\"IsActive\" IS NULL)",
        # Hide products that have been deactivated at product level by admin.
        "(p.\"IsActive\" = true OR p.\"IsActive\" IS NULL)",
        # Ensure the product has AT LEAST ONE active, non-deleted variant.
        # This prevents a product with ALL hidden variants from disappearing
        # due to a count-of-zero issue while still being correct.
        """EXISTS (
            SELECT 1 FROM twam.\"ProductVariants\" pv2
            WHERE pv2.\"ProductId\" = p.\"ProductId\"
              AND (pv2.\"DeletedInd\" = false OR pv2.\"DeletedInd\" IS NULL)
              AND (pv2.\"IsActive\" = true OR pv2.\"IsActive\" IS NULL)
        )""",
        # Hide products whose parent category has been deleted or made inactive.
        # pc is LEFT JOINed so if CategoryId is NULL we allow it through.
        "(p.\"CategoryId\" IS NULL OR (pc.\"DeletedInd\" = false AND pc.\"IsActive\" = true))",
        # Hide products whose child category has been deleted or made inactive.
        # cc is LEFT JOINed so if ChildCategoryId is NULL we allow it through.
        "(p.\"ChildCategoryId\" IS NULL OR (cc.\"DeletedInd\" = false AND cc.\"IsActive\" = true))",
        # Hide products whose brand has been deactivated (hidden) or soft-deleted.
        # b is LEFT JOINed so products with no brand (BrandId IS NULL) are always shown.
        "(p.\"BrandId\" IS NULL OR (b.\"isActive\" = true AND b.\"deletedInd\" = false))",
    ]
    params: dict = {}

    # ── State ──────────────────────────────────────────────────────────────────
    state = _fv("State", "")
    if state and state not in ("0", "", "null"):
        conds.append(
            "(pvd.\"State\" = :state OR pv.\"State\" = :state "
            "OR pvd.\"State\" IS NULL OR pv.\"State\" IS NULL)"
        )
        params["state"] = state

    # ── Category (with descendant expansion + multi-select) ───────────────────
    cat = _fv("Product.CategoryId")
    if cat and cat != "0":
        raw_cat_ids = [c.strip() for c in cat.split(",") if c.strip() and c.strip() != "0"]
        all_desc_ids: list = []
        for cid_str in raw_cat_ids:
            sub = _get_descendant_category_ids(db, cid_str)
            if sub:
                all_desc_ids.extend(sub)
            elif cid_str.isdigit():
                all_desc_ids.append(int(cid_str))
        seen: set = set()
        desc_ids = []
        for cid in all_desc_ids:
            if cid not in seen:
                seen.add(cid)
                desc_ids.append(cid)
        if len(desc_ids) == 1:
            params["cat"] = str(desc_ids[0])
            conds.append("(p.\"CategoryId\"::TEXT = :cat OR p.\"ChildCategoryId\"::TEXT = :cat)")
        elif desc_ids:
            in_ph = ", ".join(f":cat_{i}" for i in range(len(desc_ids)))
            for i, cid in enumerate(desc_ids):
                params[f"cat_{i}"] = str(cid)
            conds.append(f"(p.\"CategoryId\"::TEXT IN ({in_ph}) OR p.\"ChildCategoryId\"::TEXT IN ({in_ph}))")

    # ── Brand (multi-select) ──────────────────────────────────────────────────
    brand = _fv("Product.BrandId")
    if brand and brand != "0":
        brand_ids = [b.strip() for b in brand.split(",") if b.strip() and b.strip() != "0"]
        if len(brand_ids) == 1:
            conds.append("p.\"BrandId\"::TEXT = :brand")
            params["brand"] = brand_ids[0]
        elif len(brand_ids) > 1:
            in_ph = ", ".join(f":brand_{i}" for i in range(len(brand_ids)))
            for i, bid in enumerate(brand_ids):
                params[f"brand_{i}"] = bid
            conds.append(f"p.\"BrandId\"::TEXT IN ({in_ph})")

    # ── Band/alpha size (multi-select) ────────────────────────────────────────
    sz = _fv("Size")
    sz_ids = [
        s.strip() for s in sz.split(",")
        if s.strip() and s.strip() != "0"
    ] if sz and sz != "0" else []

    cup_sz = _fv("CupSize")
    cup_ids = [
        s.strip() for s in cup_sz.split(",")
        if s.strip() and s.strip() != "0"
    ] if cup_sz and cup_sz != "0" else []

    if len(sz_ids) == 1:
        conds.append('pvd."Size"::TEXT = :sz')
        params["sz"] = sz_ids[0]
    elif len(sz_ids) > 1:
        in_ph = ", ".join(f":sz_{i}" for i in range(len(sz_ids)))
        for i, sid in enumerate(sz_ids):
            params[f"sz_{i}"] = sid
        conds.append(f'pvd."Size"::TEXT IN ({in_ph})')

    if cup_ids:
        if sz_ids:
            if len(cup_ids) == 1:
                conds.append('(pvd."CupSize" IS NULL OR pvd."CupSize"::TEXT = :cup_sz)')
                params["cup_sz"] = cup_ids[0]
            else:
                in_ph = ", ".join(f":cup_sz_{i}" for i in range(len(cup_ids)))
                for i, cid in enumerate(cup_ids):
                    params[f"cup_sz_{i}"] = cid
                conds.append(f'(pvd."CupSize" IS NULL OR pvd."CupSize"::TEXT IN ({in_ph}))')
        elif len(cup_ids) == 1:
            conds.append('pvd."CupSize"::TEXT = :cup_sz')
            params["cup_sz"] = cup_ids[0]
        else:
            in_ph = ", ".join(f":cup_sz_{i}" for i in range(len(cup_ids)))
            for i, cid in enumerate(cup_ids):
                params[f"cup_sz_{i}"] = cid
            conds.append(f'pvd."CupSize"::TEXT IN ({in_ph})')

    color_family = _fv("ColorFamilyId") or _fv("colorFamilyId")
    color_family_ids = [
        value.strip()
        for value in color_family.split(",")
        if value.strip() and value.strip() != "0"
    ] if color_family and color_family != "0" else []
    if color_family_ids:
        in_ph = ", ".join(f":color_family_{i}" for i in range(len(color_family_ids)))
        for i, value in enumerate(color_family_ids):
            params[f"color_family_{i}"] = value
        conds.append(f'pv."ColorFamilyId"::TEXT IN ({in_ph})')

    # ── Quick search (multi-term AND logic) ───────────────────────────────────
    qs = _fv("QuickSearch")
    if qs:
        qs = qs.strip()
        terms = [t.strip() for t in qs.split() if t.strip()]

        def _term_cond(term: str, idx: int) -> str:
            norm     = term.replace("-", "").replace("_", "")
            pkey     = f"qs{idx}"
            pnorm    = f"qsnorm{idx}"
            pstart   = f"qsstart{idx}"
            params[pkey]   = f"%{term}%"
            params[pnorm]  = f"%{norm}%"
            params[pstart] = f"{term}%"
            return f"""(
                p."Name"                                                    ILIKE :{pkey}
                OR REPLACE(REPLACE(p."Name", '-', ''), ' ', '')            ILIKE :{pnorm}
                OR p."Description"                                          ILIKE :{pkey}
                OR p."Tag"                                                  ILIKE :{pkey}
                OR b."brandName"                                            ILIKE :{pkey}
                OR cf."ColorFamilyName"                                     ILIKE :{pkey}
                OR pc."Name"                                                ILIKE :{pkey}
                OR cc."Name"                                                ILIKE :{pkey}
                OR pv."VariantName"                                         ILIKE :{pkey}
                OR REPLACE(REPLACE(pv."VariantName", '-', ''), ' ', '')    ILIKE :{pnorm}
                OR pv."Color"                                               ILIKE :{pkey}
                OR s."SizeLabel"                                            ILIKE :{pkey}
                OR cs."SizeLabel"                                           ILIKE :{pkey}
                OR p."Name"                                                 ILIKE :{pstart}
            )"""

        if len(terms) == 1 and len(terms[0]) == 1:
            t = terms[0]
            params["qs_start"] = f"{t}%"
            conds.append("""(
                p."Name"            ILIKE :qs_start
                OR pv."VariantName" ILIKE :qs_start
                OR pv."Color"       ILIKE :qs_start
                OR cf."ColorFamilyName" ILIKE :qs_start
                OR s."SizeLabel"    ILIKE :qs_start
                OR cs."SizeLabel"   ILIKE :qs_start
            )""")
        else:
            for i, term in enumerate(terms):
                conds.append(_term_cond(term, i))

    # ── Best seller ───────────────────────────────────────────────────────────
    if _fv("IsBestSeller", "false").lower() in ("true", "1"):
        conds.append("pv.\"IsBestSeller\" = true")

    # ── Price range ───────────────────────────────────────────────────────────
    # FIX: comparison type for price filters is 'eq' in frontend — backend
    # reads value via _fv() which ignores comparison, so both 'eq' and 'in' work.
    minp = _fv("MinimamPrice")
    if minp and minp != "0":
        conds.append("pvd.\"FinalPrice\" >= :minp::NUMERIC")
        params["minp"] = minp

    maxp = _fv("MaxmamPrice")
    if maxp and maxp != "0":
        conds.append("pvd.\"FinalPrice\" <= :maxp::NUMERIC")
        params["maxp"] = maxp

    # ── Pagination ────────────────────────────────────────────────────────────
    where   = " AND ".join(conds)
    p_index = max(1, page_index or 1)
    p_size  = max(1, page_size  or 12)
    offset  = (p_index - 1) * p_size
    params.update({"lim": p_size, "off": offset})

    # ── Sort order ────────────────────────────────────────────────────────────
    # Default = newest first using CreatedDate (more reliable than ModifiedDate
    # which can be NULL when a product has never been edited).
    direction = "ASC" if order_ascending else "DESC"
    if order_property in ("Name", "product.name"):
        order_col = f'"_sort_name" {direction} NULLS LAST'
    elif order_property in ("FinalPrice", "Price"):
        order_col = f'"_sort_price" {direction} NULLS LAST'
    elif order_property in ("Rating", "rating"):
        order_col = f'"_sort_rating" {direction} NULLS LAST'
    else:
        # "Latest" — use COALESCE(ModifiedDate, CreatedDate) DESC
        order_col = f'COALESCE("_sort_modified", "_sort_created") {direction} NULLS LAST'

    # ── Count ─────────────────────────────────────────────────────────────────
    count_sql = f"""
        SELECT COUNT(DISTINCT pv."ProductVariantId")
        FROM twam."ProductVariants" pv
        JOIN twam."Products" p ON p."ProductId" = pv."ProductId"
        LEFT JOIN twam."ProductVariantDetail" pvd
          ON pvd."ProductVariantId" = pv."ProductVariantId"
         AND (pvd."DeletedInd" = false OR pvd."DeletedInd" IS NULL)
        LEFT JOIN mdm."Size" s         ON s."SizeId"      = pvd."Size"
        LEFT JOIN mdm."CupSize" cs     ON cs."CupSizeId"  = pvd."CupSize"
        LEFT JOIN mdm."Brand" b         ON b."brandId"     = p."BrandId"
        LEFT JOIN mdm."ProductColor" cf ON cf."ProductColorId" = pv."ColorFamilyId"
         AND (cf."DeletedInd" = false OR cf."DeletedInd" IS NULL)
        LEFT JOIN twam."Category" pc   ON pc."CategoryId"  = p."CategoryId"
        LEFT JOIN twam."Category" cc   ON cc."CategoryId"  = p."ChildCategoryId"
        WHERE {where}
    """
    try:
        total_row = db.execute(text(count_sql), params).fetchone()
        total = int(total_row[0]) if total_row else 0
    except Exception:
        import traceback; traceback.print_exc()
        total = 0

    # ── Data ──────────────────────────────────────────────────────────────────
    # All sort-key columns are given explicit aliases so the outer ORDER BY
    # can reference them by name without ambiguity.
    data_sql = f"""
        SELECT * FROM (
            SELECT DISTINCT ON (pv."ProductVariantId")
                pv."ProductVariantId"                                   AS pv_id,
                pvd."ProductVariantDetailId"                            AS pvd_id,
                p."ProductId"                                           AS product_id,
                p."ProductCode"                                         AS product_code,
                p."FabricId"                                            AS fabric_id,
                pvd."Size"                                              AS size_id,
                pvd."CupSize"                                           AS cup_size_id,
                COALESCE(pv."IsCupSize", false)                         AS is_cup_size,
                COALESCE(s."SizeLabel", '')                             AS size_label,
                COALESCE(pv."Color", '')                                AS color,
                pv."ColorFamilyId"                                      AS color_family_id,
                COALESCE(cf."ColorFamilyName", '')                      AS color_family,
                COALESCE(cf."HexCode", '')                              AS color_family_hex,
                COALESCE(pvd."StockQuantity", 0)::INTEGER               AS stock_qty,
                COALESCE(pvd."ProcessedQuantity", 0)::INTEGER           AS processed_qty,
                COALESCE(pvd."DiscountPercent", 0)::INTEGER             AS discount_pct,
                pvd."MRPPrice"                                          AS mrp_price,
                pvd."FinalPrice"                                        AS final_price,
                COALESCE(pv."UserProfileId", '')                        AS user_profile_id,
                COALESCE(pvd."State", pv."State", 'Approved')           AS state,
                COALESCE(cs."SizeLabel", '')                            AS cup_size_label,
                COALESCE(pv."IsBestSeller", false)                      AS is_best_seller,
                0                                                       AS reserved_col,
                COALESCE(pvd."FinalPrice", pvd."MRPPrice")              AS price,
                pvd."MRPPrice"                                          AS old_price,
                (SELECT pi2."FilePath"
                 FROM twam."ProductImage" pi2
                 WHERE pi2."ProductVariantId" = pv."ProductVariantId"
                   AND (pi2."DeletedInd" = false OR pi2."DeletedInd" IS NULL)
                   AND pi2."FilePath" IS NOT NULL
                 ORDER BY pi2."ProductImageId" LIMIT 1)                 AS image,
                (SELECT ARRAY_AGG(
                       pi3."FilePath"
                       ORDER BY pi3."ProductImageId")
                 FROM twam."ProductImage" pi3
                 WHERE pi3."ProductVariantId" = pv."ProductVariantId"
                   AND (pi3."DeletedInd" = false OR pi3."DeletedInd" IS NULL)
                   AND pi3."FilePath" IS NOT NULL)                       AS images_arr,
                COALESCE(p."Name", '')                                  AS name,
                COALESCE(NULLIF(pv."VariantDescription", ''), p."Description", '') AS description,
                COALESCE(
                    (SELECT ROUND(AVG(pr."Rating"::NUMERIC), 1)
                     FROM twam."ProductReview" pr
                     WHERE pr."ProductVariantId" = pv."ProductVariantId"
                       AND (pr."DeletedInd" = false OR pr."DeletedInd" IS NULL)),
                    0
                )                                                       AS _sort_rating,
                COALESCE(
                    (SELECT COUNT(pr2."ProductReviewId")
                     FROM twam."ProductReview" pr2
                     WHERE pr2."ProductVariantId" = pv."ProductVariantId"
                       AND (pr2."DeletedInd" = false OR pr2."DeletedInd" IS NULL)),
                    0
                )                                                       AS review_count,
                pv."ModifiedDate"                                       AS _sort_modified,
                COALESCE(pvd."FinalPrice", pvd."MRPPrice")              AS _sort_price,
                p."Name"                                                AS _sort_name,
                b."brandName"                                           AS brand_name,
                p."BrandId"                                             AS brand_id,
                pv."CreatedDate"                                        AS _sort_created
            FROM twam."ProductVariants" pv
            JOIN twam."Products" p ON p."ProductId" = pv."ProductId"
            LEFT JOIN twam."ProductVariantDetail" pvd
              ON pvd."ProductVariantId" = pv."ProductVariantId"
             AND (pvd."DeletedInd" = false OR pvd."DeletedInd" IS NULL)
            LEFT JOIN mdm."Size" s         ON s."SizeId"     = pvd."Size"
            LEFT JOIN mdm."CupSize" cs     ON cs."CupSizeId" = pvd."CupSize"
            LEFT JOIN mdm."Brand" b         ON b."brandId"    = p."BrandId"
            LEFT JOIN mdm."ProductColor" cf ON cf."ProductColorId" = pv."ColorFamilyId"
             AND (cf."DeletedInd" = false OR cf."DeletedInd" IS NULL)
            LEFT JOIN twam."Category" pc   ON pc."CategoryId"  = p."CategoryId"
            LEFT JOIN twam."Category" cc   ON cc."CategoryId"  = p."ChildCategoryId"
            WHERE {where}
            ORDER BY
                pv."ProductVariantId",
                CASE WHEN COALESCE(pvd."AvailableQuantity", pvd."StockQuantity", 0) > 0 THEN 0 ELSE 1 END,
                COALESCE(pvd."FinalPrice", pvd."MRPPrice") ASC NULLS LAST,
                pvd."ProductVariantDetailId" ASC NULLS LAST
        ) deduped
        ORDER BY {order_col}
        LIMIT :lim OFFSET :off
    """
    try:
        rows = db.execute(text(data_sql), params).fetchall()
    except Exception:
        import traceback; traceback.print_exc()
        return {"count": 0, "list": []}

    def _f(val):
        """Safe float ? returns None for None, actual float (incl 0.0) otherwise."""
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    items = []
    for r in rows:
        m = r._mapping
        items.append({
            "productVariantId":  m["pv_id"],
            "productVariantDetailId": m["pvd_id"],
            "productId":         m["product_id"],
            "productCode":       m["product_code"],
            "fabricId":          m["fabric_id"],
            "sizeId":            m["size_id"],
            "size":              m["size_label"],
            "color":             m["color"],
            "colorName":         m["color"],
            "colorFamilyId":     m["color_family_id"],
            "colorFamily":       m["color_family"],
            "colorFamilyHex":    m["color_family_hex"],
            "colorHex":          m["color_family_hex"],
            "stockQuantity":     m["stock_qty"],
            "processedQuantity": m["processed_qty"],
            "discountPercent":   m["discount_pct"],
            "mrpPrice":          _f(m["mrp_price"]),
            "finalPrice":        _f(m["final_price"]),
            "userProfileId":     m["user_profile_id"],
            "state":             m["state"],
            "cupSizeId":         m["cup_size_id"],
            "cupSize":           m["cup_size_label"],
            "isCupSize":         bool(m["is_cup_size"]) if m["is_cup_size"] is not None else None,
            "isBestSeller":      m["is_best_seller"],
            "totalCount":        total,
            "price":             _f(m["price"]),
            "oldPrice":          _f(m["old_price"]),
            "image":             _resolve_url(m["image"]),
            "images":            [
                _resolve_url(img)
                for img in (list(m["images_arr"]) if m["images_arr"] else ([m["image"]] if m["image"] else []))
                if img
            ],
            "name":              m["name"],
            "description":       m["description"],
            "rating":            _f(m["_sort_rating"]),
            "reviewCount":       int(m["review_count"]) if m["review_count"] is not None else 0,
            "brandName":         m["brand_name"],
            "brandId":           m["brand_id"],
        })

    # Color results returned in DB order (ColorFamily-based grouping handles relevance)

    return {"count": total, "list": items}

# ── New Arrivals — products added in the last 6 months ────────────────────────

def get_new_arrivals(
    db: Session,
    page_index: int = 1,
    page_size: int = 40,
) -> dict:
    """
    Return product variants whose ProductVariants.CreatedDate falls within the
    last 6 months, ordered newest-first.  Only active/non-deleted variants with
    at least one non-deleted ProductVariantDetail are returned.
    """
    p_index = max(1, page_index)
    p_size  = max(1, page_size)
    offset  = (p_index - 1) * p_size

    params = {"lim": p_size, "off": offset}

    where = """
        (pv."DeletedInd" = false OR pv."DeletedInd" IS NULL)
        AND (p."DeletedInd"  = false OR p."DeletedInd"  IS NULL)
        AND (pv."IsActive" = true OR pv."IsActive" IS NULL)
        AND pv."CreatedDate" >= NOW() - INTERVAL '6 months'
        AND (p."CategoryId" IS NULL OR (pc."DeletedInd" = false AND pc."IsActive" = true))
        AND (p."ChildCategoryId" IS NULL OR (cc."DeletedInd" = false AND cc."IsActive" = true))
        AND (p."BrandId" IS NULL OR (b."isActive" = true AND b."deletedInd" = false))
    """

    count_sql = f"""
        SELECT COUNT(DISTINCT pv."ProductVariantId")
        FROM twam."ProductVariants" pv
        JOIN twam."Products" p ON p."ProductId" = pv."ProductId"
        LEFT JOIN twam."ProductVariantDetail" pvd
          ON pvd."ProductVariantId" = pv."ProductVariantId"
         AND (pvd."DeletedInd" = false OR pvd."DeletedInd" IS NULL)
        LEFT JOIN mdm."Brand" b ON b."brandId" = p."BrandId"
        LEFT JOIN mdm."ProductColor" cf ON cf."ProductColorId" = pv."ColorFamilyId"
         AND (cf."DeletedInd" = false OR cf."DeletedInd" IS NULL)
        LEFT JOIN twam."Category" pc ON pc."CategoryId"  = p."CategoryId"
        LEFT JOIN twam."Category" cc ON cc."CategoryId"  = p."ChildCategoryId"
        WHERE {where}
    """

    try:
        total_row = db.execute(text(count_sql), params).fetchone()
        total = int(total_row[0]) if total_row else 0
    except Exception:
        import traceback; traceback.print_exc()
        total = 0

    data_sql = f"""
        SELECT * FROM (
            SELECT DISTINCT ON (pv."ProductVariantId")
                pv."ProductVariantId"                                   AS pv_id,
                p."ProductId"                                           AS product_id,
                p."ProductCode"                                         AS product_code,
                p."FabricId"                                            AS fabric_id,
                COALESCE(s."SizeLabel", '')                             AS size_label,
                COALESCE(pv."Color", '')                                AS color,
                pv."ColorFamilyId"                                      AS color_family_id,
                COALESCE(cf."ColorFamilyName", '')                      AS color_family,
                COALESCE(cf."HexCode", '')                              AS color_family_hex,
                COALESCE(pvd."StockQuantity", 0)::INTEGER               AS stock_qty,
                COALESCE(pvd."ProcessedQuantity", 0)::INTEGER           AS processed_qty,
                COALESCE(pvd."DiscountPercent", 0)::INTEGER             AS discount_pct,
                pvd."MRPPrice"                                          AS mrp_price,
                pvd."FinalPrice"                                        AS final_price,
                COALESCE(pv."UserProfileId", '')                        AS user_profile_id,
                COALESCE(pvd."State", pv."State", 'Approved')           AS state,
                COALESCE(cs."SizeLabel", '')                            AS cup_size_label,
                COALESCE(pv."IsBestSeller", false)                      AS is_best_seller,
                0                                                       AS reserved_col,
                COALESCE(pvd."FinalPrice", pvd."MRPPrice")              AS price,
                pvd."MRPPrice"                                          AS old_price,
                (SELECT pi2."FilePath"
                 FROM twam."ProductImage" pi2
                 WHERE pi2."ProductVariantId" = pv."ProductVariantId"
                   AND (pi2."DeletedInd" = false OR pi2."DeletedInd" IS NULL)
                   AND pi2."FilePath" IS NOT NULL
                 ORDER BY pi2."ProductImageId" LIMIT 1)                 AS image,
                (SELECT ARRAY_AGG(
                       pi3."FilePath"
                       ORDER BY pi3."ProductImageId")
                 FROM twam."ProductImage" pi3
                 WHERE pi3."ProductVariantId" = pv."ProductVariantId"
                   AND (pi3."DeletedInd" = false OR pi3."DeletedInd" IS NULL)
                   AND pi3."FilePath" IS NOT NULL)                       AS images_arr,
                COALESCE(p."Name", '')                                  AS name,
                COALESCE(NULLIF(pv."VariantDescription", ''), p."Description", '') AS description,
                COALESCE(
                    (SELECT ROUND(AVG(pr."Rating"::NUMERIC), 1)
                     FROM twam."ProductReview" pr
                     WHERE pr."ProductVariantId" = pv."ProductVariantId"
                       AND (pr."DeletedInd" = false OR pr."DeletedInd" IS NULL)),
                    0
                )                                                       AS rating,
                COALESCE(
                    (SELECT COUNT(pr2."ProductReviewId")
                     FROM twam."ProductReview" pr2
                     WHERE pr2."ProductVariantId" = pv."ProductVariantId"
                       AND (pr2."DeletedInd" = false OR pr2."DeletedInd" IS NULL)),
                    0
                )                                                       AS review_count,
                pv."CreatedDate"                                        AS created_date,
                COALESCE(pvd."FinalPrice", pvd."MRPPrice")              AS sort_price,
                p."Name"                                                AS sort_name,
                b."brandName"                                           AS brand_name,
                p."BrandId"                                             AS brand_id,
                pvd."ProductVariantDetailId"                            AS pvd_id
            FROM twam."ProductVariants" pv
            JOIN twam."Products" p ON p."ProductId" = pv."ProductId"
            LEFT JOIN twam."ProductVariantDetail" pvd
              ON pvd."ProductVariantId" = pv."ProductVariantId"
             AND (pvd."DeletedInd" = false OR pvd."DeletedInd" IS NULL)
            LEFT JOIN mdm."Size" s     ON s."SizeId"     = pvd."Size"
            LEFT JOIN mdm."CupSize" cs ON cs."CupSizeId" = pvd."CupSize"
            LEFT JOIN mdm."Brand" b     ON b."brandId"    = p."BrandId"
            LEFT JOIN mdm."ProductColor" cf ON cf."ProductColorId" = pv."ColorFamilyId"
             AND (cf."DeletedInd" = false OR cf."DeletedInd" IS NULL)
            LEFT JOIN twam."Category" pc ON pc."CategoryId"  = p."CategoryId"
            LEFT JOIN twam."Category" cc ON cc."CategoryId"  = p."ChildCategoryId"
            WHERE {where}
            ORDER BY
                pv."ProductVariantId",
                CASE WHEN COALESCE(pvd."AvailableQuantity", pvd."StockQuantity", 0) > 0 THEN 0 ELSE 1 END,
                COALESCE(pvd."FinalPrice", pvd."MRPPrice") ASC NULLS LAST,
                pvd."ProductVariantDetailId" ASC NULLS LAST
        ) deduped
        ORDER BY created_date DESC NULLS LAST
        LIMIT :lim OFFSET :off
    """

    try:
        rows = db.execute(text(data_sql), params).fetchall()
    except Exception:
        import traceback; traceback.print_exc()
        return {"count": 0, "list": []}

    def _f(val):
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    items = []
    for r in rows:
        m = r._mapping
        items.append({
            "productVariantId":  m["pv_id"],
            "productVariantDetailId": m["pvd_id"],
            "productId":         m["product_id"],
            "productCode":       m["product_code"],
            "fabricId":          m["fabric_id"],
            "size":              m["size_label"],
            "color":             m["color"],
            "colorName":         m["color"],
            "colorFamilyId":     m["color_family_id"],
            "colorFamily":       m["color_family"],
            "colorFamilyHex":    m["color_family_hex"],
            "colorHex":          m["color_family_hex"],
            "stockQuantity":     m["stock_qty"],
            "processedQuantity": m["processed_qty"],
            "discountPercent":   m["discount_pct"],
            "mrpPrice":          _f(m["mrp_price"]),
            "finalPrice":        _f(m["final_price"]),
            "userProfileId":     m["user_profile_id"],
            "state":             m["state"],
            "cupSize":           m["cup_size_label"],
            "isBestSeller":      m["is_best_seller"],
            "totalCount":        total,
            "price":             _f(m["price"]),
            "oldPrice":          _f(m["old_price"]),
            "image":             _resolve_url(m["image"]),
            "images":            [
                _resolve_url(img)
                for img in (list(m["images_arr"]) if m["images_arr"] else ([m["image"]] if m["image"] else []))
                if img
            ],
            "name":              m["name"],
            "description":       m["description"],
            "rating":            _f(m["rating"]),
            "reviewCount":       int(m["review_count"]) if m["review_count"] is not None else 0,
            "brandName":         m["brand_name"],
            "brandId":           m["brand_id"],
        })

    return {"count": total, "list": items}