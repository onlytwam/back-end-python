"""
app/bulkimport/router.py
─────────────────────────
Single unified Bulk Import router.

Mounts at:  /api/BulkImport

───────────────────────────────────────────────────────────────────────────────
Generic entities  (Products-simple, Category, brands, Size, Coupons)
───────────────────────────────────────────────────────────────────────────────
  GET  /api/BulkImport/entities                        list supported entities
  GET  /api/BulkImport/template/{entity}               download Excel template
  POST /api/BulkImport/preview/{entity}                validate, no DB write
  POST /api/BulkImport/import/{entity}                 insert valid rows

───────────────────────────────────────────────────────────────────────────────
Product Bulk  (Products + ProductVariants + ProductVariantDetail, flat sheet)
───────────────────────────────────────────────────────────────────────────────
  GET  /api/BulkImport/product-bulk/template           download flat template
  POST /api/BulkImport/product-bulk/preview            validate all rows
  POST /api/BulkImport/product-bulk/import             insert (full rollback on DB error)

Sheet layout for product-bulk  (one row = one variant):
  ┌─ Product columns ─────────────────────────────────────────────────┐
  │ Product Code* | Product Name* | Description* | Category ID*       │
  │ Brand ID* | Child Category ID | Tag | TAX HSN Code               │
  ├─ Variant columns ─────────────────────────────────────────────────┤
  │ Product Code* | Variant Name* | Variant Description | Fabric ID   │
  │ Color | Is Best Seller | Is Return Available | Is Cup Size        │
  │ Variant TAX HSN Code                                              │
  ├─ Size slots (repeated MAX_SIZES times) ───────────────────────────┤
  │ Size ID n | Cup Size ID n | MRP Price n | Final Price n           │
  │ Stock Qty n                                                        │
  └───────────────────────────────────────────────────────────────────┘
"""

import io
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.formatting.rule import FormulaRule

try:
    import openpyxl
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    OPENPYXL_AVAILABLE = True
except ImportError:
    OPENPYXL_AVAILABLE = False

from database import get_db
from app.auth.dependencies import get_current_user, Roles, CurrentUser

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
#  Router
# ══════════════════════════════════════════════════════════════════════════════

router = APIRouter(
    prefix="/api/BulkImport",
    tags=["Bulk Import"],
    dependencies=[Depends(get_current_user)],
)

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731

# ══════════════════════════════════════════════════════════════════════════════
#  Shared helpers
# ══════════════════════════════════════════════════════════════════════════════

def _check_openpyxl():
    if not OPENPYXL_AVAILABLE:
        raise HTTPException(
            status_code=500,
            detail="openpyxl is not installed. Run: pip install openpyxl",
        )


# raising for invalid values; also handles numeric 1/0 passed from Excel.
def _cast(value: Any, dtype: str) -> Any:
    """Cast raw cell value to Python type. Returns None for blank."""
    if value is None or str(value).strip() == "":
        return None
    raw = str(value).strip()
    try:
        if dtype == "int":
            return int(float(raw))
        if dtype == "float":
            return float(raw)
        if dtype == "bool":
            val = raw.strip().upper()
            if val in ("TRUE", "1", "YES", "Y"):
                return True
            if val in ("FALSE", "0", "NO", "N"):
                return False
            # FIX: return None (not raise) so the caller can decide whether
            # to surface an error; this prevents false "isCupSize required"
            # errors when the cell simply has an unexpected truthy/falsy string.
            return None
        return raw
    except (ValueError, TypeError):
        raise ValueError(f"Cannot cast '{value}' to {dtype}")


def _thin_border() -> Border:
    s = Side(style="thin", color="DDDDDD")
    return Border(left=s, right=s, top=s, bottom=s)


def _center_align() -> Alignment:
    return Alignment(horizontal="center", vertical="center", wrap_text=True)


# ══════════════════════════════════════════════════════════════════════════════
#  ① GENERIC ENTITY CONFIG
# ══════════════════════════════════════════════════════════════════════════════

ENTITY_CONFIG: dict[str, dict] = {
    "Products": {
        "columns": [
            ("productCode",     "Product Code*",     "str",   True),
            ("name",            "Product Name*",     "str",   True),
            ("description",     "Description*",      "str",   True),
            ("categoryId",      "Category ID*",      "int",   True),
            ("brandId",         "Brand ID*",         "int",   True),
            ("childCategoryId", "Child Category ID", "int",   False),
            # FIX #15a — "tag" column was missing, causing KeyError on import
            ("tag",             "Tags",              "str",   False),
        ],
        "required": ["productCode", "name", "description", "categoryId", "brandId"],
        "notes": "Get Category ID from Category page · Get Brand ID from Brands page",
        "sample": {
            "productCode": "PRD-001", "name": "Sample Product",
            "description": "A brief description", "categoryId": 1,
            "brandId": 1, "tag": "summer,casual", "childCategoryId": "",
        },
    },
    "Category": {
        "columns": [
            ("Name",             "Name*",              "str", True),
            ("Description",      "Description",        "str", False),
            ("ParentCategoryId", "Parent Category ID", "int", False),
        ],
        "required": ["Name"],
        "notes": "Leave Parent Category ID blank for top-level categories",
        "sample": {"Name": "Womens", "Description": "Women's category", "ParentCategoryId": ""},
    },
    "brands": {
        "columns": [
            ("brandName",        "Brand Name*",  "str", True),
            ("brandDescription", "Description",  "str", False),
        ],
        "required": ["brandName"],
        "notes": "Brand images must be uploaded separately via the Brands page",
        "sample": {"brandName": "Sample Brand", "brandDescription": "Brand description here"},
    },
    "Size": {
        "columns": [
            ("sizeLabel",   "Size Label*", "str", True),
            ("sizeCode",    "Size Code*",  "str", True),
            ("description", "Description", "str", False),
            ("dimensions",  "Dimensions",  "str", False),
            ("orderNo",     "Order No",    "int", False),
        ],
        "required": ["sizeLabel", "sizeCode"],
        "notes": "Order No controls the display order (1 = first)",
        "sample": {
            "sizeLabel": "S", "sizeCode": "S",
            "description": "", "dimensions": "Chest: 34-36in", "orderNo": 1,
        },
    },
    "Coupons": {
        "columns": [
            ("CouponCode",  "Coupon Code*",  "str",   True),
            ("CouponName",  "Coupon Name*",  "str",   True),
            ("Description", "Description",   "str",   False),
            ("Discount",    "Discount*",     "float", True),
            ("ExpiryDate",  "Expiry Date",   "str",   False),
        ],
        "required": ["CouponCode", "CouponName", "Discount"],
        "notes": "Expiry Date format: YYYY-MM-DD · Discount is a percentage value (e.g. 10 for 10%)",
        "sample": {
            "CouponCode": "SAVE10", "CouponName": "Save 10%",
            "Description": "10% off", "Discount": 10, "ExpiryDate": "2025-12-31",
        },
    },
}

# ── Generic Excel parser ───────────────────────────────────────────────────────

def _parse_entity_excel(file_bytes: bytes, entity: str) -> list[dict]:
    _check_openpyxl()
    cfg      = ENTITY_CONFIG[entity]
    col_defs = cfg["columns"]

    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Cannot read Excel file: {exc}")

    ws   = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise HTTPException(status_code=400, detail="The Excel file is empty.")

    # header → column index
    header_row = [str(h).strip().rstrip("*").strip() if h else "" for h in rows[0]]
    field_map: dict[str, int] = {}
    for col_idx, header in enumerate(header_row):
        for field, label, dtype, req in col_defs:
            clean = label.rstrip("*").strip()
            if header.lower() in (clean.lower(), field.lower()) and field not in field_map:
                field_map[field] = col_idx

    for field, label, dtype, req in col_defs:
        if req and field not in field_map:
            raise HTTPException(
                status_code=400,
                detail=f"Required column '{label}' not found. Download the template first.",
            )

    dtype_map = {f: d for f, _, d, _ in col_defs}
    result: list[dict] = []

    for row_num, row in enumerate(rows[1:], start=2):
        if all(cell is None or str(cell).strip() == "" for cell in row):
            continue
        errors: list[str] = []
        record: dict = {"_row": row_num}

        for field, label, dtype, required in col_defs:
            col_idx = field_map.get(field)
            raw     = row[col_idx] if col_idx is not None and col_idx < len(row) else None
            try:
                val = _cast(raw, dtype_map[field])
            except ValueError:
                errors.append(f"Column '{label}': invalid value '{raw}'")
                val = None
            if required and val is None:
                errors.append(f"Column '{label}' is required")
            record[field] = val

        record["_errors"] = errors
        result.append(record)

    return result


# ── Generic DB insert ──────────────────────────────────────────────────────────

def _insert_entity_row(db: Session, entity: str, row: dict, user_id: str, now: datetime):
    if entity == "Products":
        db.execute(text("""
            INSERT INTO twam."Products"
              ("ProductCode","Name","Description",
               "CategoryId","ChildCategoryId","BrandId","Tag",
               "State","UserProfileId","CreatedBy","ModifiedBy",
               "CreatedDate","ModifiedDate","DeletedInd")
            VALUES
              (:productCode,:name,:description,
               :categoryId,:childCategoryId,:brandId,:tag,
               'Created',:userId,:userId,:userId,
               :now,:now,false)
        """), {**row, "userId": user_id, "now": now})

    elif entity == "Category":
        db.execute(text("""
            INSERT INTO twam."Category"
              ("Name","Description","ParentCategoryId",
               "IsActive","CreatedBy","ModifiedBy",
               "CreatedDate","ModifiedDate","DeletedInd")
            VALUES
              (:Name,:Description,:ParentCategoryId,
               true,:userId,:userId,
               :now,:now,false)
        """), {**row, "userId": user_id, "now": now})

    elif entity == "brands":
        db.execute(text("""
            INSERT INTO mdm."brands"
              ("brandName","brandDescription","userProfileId",
               "state","isActive","createdDate","modifiedDate","deletedInd")
            VALUES
              (:brandName,:brandDescription,:userId,
               'Active',true,:now,:now,false)
        """), {**row, "userId": user_id, "now": now})

    elif entity == "Size":
        db.execute(text("""
            INSERT INTO mdm."Size"
              ("SizeLabel","SizeCode","Description","Dimensions","OrderNo",
               "State","IsActive","UserProfileId",
               "CreatedDate","ModifiedDate","DeletedInd")
            VALUES
              (:sizeLabel,:sizeCode,:description,:dimensions,:orderNo,
               'Active',true,:userId,
               :now,:now,false)
        """), {**row, "userId": user_id, "now": now})

    elif entity == "Coupons":
        def _parse_date(val):
            if not val:
                return None
            try:
                if isinstance(val, (int, float)):
                    d = datetime(1899, 12, 30) + timedelta(days=int(val))
                else:
                    d = datetime.strptime(str(val), "%Y-%m-%d")
                return d.replace(tzinfo=timezone.utc)
            except Exception:
                return None

        db.execute(text("""
            INSERT INTO twam."Coupons"
              ("CouponCode","CouponName","Description","Discount",
               "StartDate","EndDate",
               "IsActive","UserProfileId",
               "CreatedDate","ModifiedDate","DeletedInd")
            VALUES
              (:CouponCode,:CouponName,:Description,:Discount,
               :StartDate,:EndDate,
               true,:userId,
               :now,:now,false)
        """), {
            **row,
            "StartDate": None,
            "EndDate":   _parse_date(row.get("ExpiryDate")),
            "userId":    user_id,
            "now":       now,
        })
    else:
        raise ValueError(f"No insert handler for entity '{entity}'")


# ══════════════════════════════════════════════════════════════════════════════
#  ② PRODUCT-BULK CONFIG  (flat sheet → Products + Variants + VariantDetails)
# ══════════════════════════════════════════════════════════════════════════════

MAX_SIZES = 5

PRODUCT_COLS: list[tuple[str, str, str, bool]] = [
    ("productCode",    "Product Code*",     "str", True),
    ("name",           "Product Name*",     "str", True),
    ("description",    "Description*",      "str", True),
    ("categoryId",     "Category ID*",      "int", True),
    ("childCategoryId","Child Category ID", "int", False),
    ("brandId",        "Brand ID*",         "int", True),
    # FIX #15a — "tag" column added so it never raises KeyError
    ("tag",            "Tags",              "str", False),
]

VARIANT_COLS: list[tuple[str, str, str, bool]] = [
    ("variantCode",         "Product Code*",       "str",  True),
    ("variantName",         "Variant Name*",        "str",  True),
    ("variantDescription",  "Variant Description",  "str",  False),
    ("fabricId",            "Fabric ID",            "int",  False),
    ("color",               "Color",                "str",  False),
    ("isBestSeller",        "Is Best Seller",       "bool", False),
    ("isReturnAvailable",   "Is Return Available",  "bool", False),
    # FIX #4 — isCupSize is mandatory (req=True)
    ("isCupSize",           "Is Cup Size*",         "bool", True),
    ("variantTaxHSNCodeId", "Variant TAX HSN Code", "int",  False),
]

# FIX #1 — taxHSNCodeId removed from size slots; availableQuantity kept
SIZE_SLOT_COLS: list[tuple[str, str, str, bool]] = [
    ("sizeId",            "Size ID",          "int",   True),
    ("cupSizeId",         "Cup Size ID",      "int",   False),
    ("mrpPrice",          "MRP Price",        "float", True),
    ("finalPrice",        "Final Price",      "float", True),
    # FIX #1 — availableQuantity confirmed present (was missing in original)
    ("availableQuantity", "Stock Qty",        "int",   True),
    ("discountPercent",   "Discount %",       "int",   False),
]

_SIZE_FIELD_KEYS: set[str] = {
    f"{field}_{n}"
    for field, *_ in SIZE_SLOT_COLS
    for n in range(1, MAX_SIZES + 1)
}

PRODUCT_BULK_SAMPLE: dict[str, Any] = {
    "productCode":         "PRD-001",
    "name":                "Sample Product",
    "description":         "A brief description of the product",
    "categoryId":          1,
    "brandId":             1,
    "childCategoryId":     "",
    "tag":                 "",
    # variant
    "variantCode":         "VAR-001-RED",
    "variantName":         "Red Variant",
    "variantDescription":  "Red colour variant",
    "fabricId":            "",
    "color":               "Red",
    "isBestSeller":        "FALSE",
    "isReturnAvailable":   "TRUE",
    "isCupSize":           "FALSE",
    "variantTaxHSNCodeId": "",
    # size slot 1
    "sizeId_1": 2, "cupSizeId_1": "", "mrpPrice_1": 999.00,
    "finalPrice_1": 799.00, "availableQuantity_1": 50,
    # size slot 2
    "sizeId_2": 3, "cupSizeId_2": "", "mrpPrice_2": 999.00,
    "finalPrice_2": 799.00, "availableQuantity_2": 30,
}


def _all_product_bulk_headers() -> list[tuple[str, str, str, bool]]:
    cols = list(PRODUCT_COLS) + list(VARIANT_COLS)
    for n in range(1, MAX_SIZES + 1):
        for field, label, dtype, req in SIZE_SLOT_COLS:
            cols.append((f"{field}_{n}", f"{label} {n}", dtype, False))
    return cols


ALL_PRODUCT_BULK_COLS = _all_product_bulk_headers()


# ── Product-bulk Excel parser ─────────────────────────────────────────────────

def _parse_product_bulk_excel(file_bytes: bytes) -> list[dict]:
    _check_openpyxl()
    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Cannot read Excel file: {exc}")

    ws   = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise HTTPException(status_code=400, detail="The Excel file is empty.")

    # header → column index
    header_row = [str(h).strip().rstrip("*").strip() if h else "" for h in rows[0]]
    field_map: dict[str, int] = {}
    for col_idx, header in enumerate(header_row):
        for field, label, dtype, req in ALL_PRODUCT_BULK_COLS:
            clean = label.rstrip("*").strip()
            if header.lower() in (clean.lower(), field.lower()) and field not in field_map:
                field_map[field] = col_idx

    for field, label, dtype, req in PRODUCT_COLS + VARIANT_COLS:
        if req and field not in field_map:
            raise HTTPException(
                status_code=400,
                detail=f"Required column '{label}' not found. Download the product-bulk template first.",
            )

    dtype_map = {f: d for f, _, d, _ in ALL_PRODUCT_BULK_COLS}
    result: list[dict] = []

    for row_num, row in enumerate(rows[1:], start=2):
        if all(cell is None or str(cell).strip() == "" for cell in row):
            continue

        errors: list[str] = []
        record: dict = {"_row": row_num}

        for field, label, dtype, required in ALL_PRODUCT_BULK_COLS:
            col_idx = field_map.get(field)
            raw     = row[col_idx] if col_idx is not None and col_idx < len(row) else None
            try:
                val = _cast(raw, dtype_map[field])
            except ValueError:
                errors.append(f"Column '{label}': invalid value '{raw}'")
                val = None
            # FIX #4 — isCupSize required check; skip size slot keys
            if required and field not in _SIZE_FIELD_KEYS and val is None:
                errors.append(f"Column '{label}' is required")
            record[field] = val

        for n in range(1, MAX_SIZES + 1):
            mrp   = record.get(f"mrpPrice_{n}")
            final = record.get(f"finalPrice_{n}")
            if mrp is not None and final is not None and final > mrp:
                errors.append(f"Size slot {n}: Final Price ({final}) cannot exceed MRP Price ({mrp})")

        has_any_size = any(
            all(record.get(f"{f}_{n}") is not None
                for f in ("sizeId", "mrpPrice", "finalPrice", "availableQuantity"))
            for n in range(1, MAX_SIZES + 1)
        )
        if not has_any_size:
            errors.append(
                "At least one complete size slot (Size ID, MRP Price, Final Price, Stock Qty) is required"
            )

        is_cup = record.get("isCupSize")
        # Normalise: _cast returns True/False/None; treat None as False
        if is_cup is None:
            is_cup = False
        for n in range(1, MAX_SIZES + 1):
            cup_id = record.get(f"cupSizeId_{n}")
            sid    = record.get(f"sizeId_{n}")
            if sid is None:
                continue  # empty slot — skip
            if is_cup and not cup_id:
                errors.append(f"Size slot {n}: Is Cup Size = TRUE but Cup Size ID is missing")
            if not is_cup and cup_id:
                errors.append(f"Size slot {n}: Is Cup Size = FALSE but Cup Size ID should not be provided")

        record["_errors"] = errors
        record["_valid"]  = len(errors) == 0
        result.append(record)

    return result


def _extract_valid_size_slots(record: dict) -> list[dict]:
    slots = []
    for n in range(1, MAX_SIZES + 1):
        sid   = record.get(f"sizeId_{n}")
        mrp   = record.get(f"mrpPrice_{n}")
        final = record.get(f"finalPrice_{n}")
        stock = record.get(f"availableQuantity_{n}")
        if any(v is None for v in (sid, mrp, final, stock)):
            continue
        # FIX #7 — stock logic
        is_out = stock == 0
        is_low = not is_out and stock < 10
        slots.append({
            "sizeId":             sid,
            "cupSizeId":          record.get(f"cupSizeId_{n}"),
            "mrpPrice":           mrp,
            "finalPrice":         final,
            "availableQuantity":  stock,
            "discountPercent":    record.get(f"discountPercent_{n}") or 0,
            "isLowStock":         is_low,
            "isOutOfStock":       is_out,
        })
    return slots


def _is_sample_row(rec: dict) -> bool:
    pcode = str(rec.get("productCode") or "").strip()
    vcode = str(rec.get("variantCode") or "").strip()
    name  = str(rec.get("name") or "").strip().lower()

    if pcode in {"PRD-001", "PRD-002"}:
        return True
    if vcode in {"VAR-RED", "VAR-BLUE", "VAR-GREEN"}:
        return True
    if "product 1" in name or "product 2" in name:
        return True

    return False


def _build_valid_chains(rows: list[dict]) -> dict[str, dict]:
    """Flat parser chains — group by productCode (flat sheet has no _p_row)."""
    chains: dict[str, dict] = {}

    for rec in rows:
        if not rec["_valid"]:
            continue
        # FIX #2 — skip sample rows in flat sheet too
        if _is_sample_row(rec):
            continue

        slots = _extract_valid_size_slots(rec)
        if not slots:
            continue

        pcode = rec.get("productCode", "unknown")
        vcode = rec.get("variantCode", "unknown")

        pkey = pcode
        vkey = vcode

        if pkey not in chains:
            chains[pkey] = {"rec": rec, "variants": {}}

        if vkey not in chains[pkey]["variants"]:
            chains[pkey]["variants"][vkey] = {"rec": rec, "slots": slots}

    result = {}
    for pkey, chain in chains.items():
        variant_list = [v for v in chain["variants"].values() if v["slots"]]
        if variant_list:
            result[pkey] = {"rec": chain["rec"], "variants": variant_list}

    return result


# ── Product-bulk SQL inserts ───────────────────────────────────────────────────

def _pb_insert_product(db: AsyncSession, rec: dict) -> int:
    r = db.execute(text("""
        INSERT INTO twam."Products" (
            "ProductCode","Name","Description",
            "CategoryId","ChildCategoryId","BrandId",
            "Tag",
            "State","CreatedDate","DeletedInd"
        ) VALUES (
            :productCode,:name,:description,
            :categoryId,:childCategoryId,:brandId,
            :tag,
            'Created',:now,false
        ) RETURNING "ProductId"
    """), {
        "productCode":     rec["productCode"],
        "name":            rec["name"],
        "description":     rec["description"],
        "categoryId":      rec["categoryId"],
        "childCategoryId": rec.get("childCategoryId"),
        "brandId":         rec["brandId"],
        # FIX #15a — tag safely accessed with .get() to avoid KeyError
        "tag":             rec.get("tag"),
        "now":             NOW(),
    })
    return r.scalar_one()


def _pb_insert_variant(db: AsyncSession, rec: dict, product_id: int) -> int:
    r = db.execute(text("""
        INSERT INTO twam."ProductVariants" (
            "ProductId","ProductCode",
            "VariantName","VariantDescription",
            "FabricId","TaxHSNCodeId","Color",
            "IsBestSeller","IsReturnAvailable","IsCupSize","CreatedDate"
        ) VALUES (
            :productId,:variantCode,
            :variantName,:variantDescription,
            :fabricId,:taxHSNCodeId,:color,
            :isBestSeller,:isReturnAvailable,:isCupSize,
            :now
        ) RETURNING "ProductVariantId"
    """), {
        "productId":          product_id,
        "variantCode":        rec["variantCode"],
        "variantName":        rec["variantName"],
        "variantDescription": rec.get("variantDescription"),
        "fabricId":           rec.get("fabricId"),
        "taxHSNCodeId":       rec.get("variantTaxHSNCodeId"),
        "color":              rec.get("color"),
        "isBestSeller":       rec.get("isBestSeller") or False,
        "isReturnAvailable":  rec.get("isReturnAvailable") or False,
        "isCupSize":          rec.get("isCupSize") or False,
        "now":                NOW(),
    })
    return r.scalar_one()


def _pb_insert_detail(
    db: AsyncSession, slot: dict,
    product_id: int, variant_id: int,
    variant_code: str,
) -> int:
    r = db.execute(text("""
        INSERT INTO twam."ProductVariantDetail" (
            "ProductId","ProductVariantId","ProductCode",
            "Size","CupSize",
            "AvailableQuantity",
            "ProcessedQuantity","ReturnedQuantity","AmendmentQuantity",
            "MRPPrice","FinalPrice","DiscountPercent",
            "IsLowStock","IsOutOfStock",
            "State","CreatedDate","DeletedInd"
        ) VALUES (
            :productId,:productVariantId,:productCode,
            :size,:cupSize,
            :availableQuantity,
            0,0,0,
            :mrpPrice,:finalPrice,:discountPercent,
            :isLowStock,:isOutOfStock,
            'Created',:now,false
        )
        RETURNING "ProductVariantDetailId"
    """), {
        "productId":         product_id,
        "productVariantId":  variant_id,
        "productCode":       variant_code,
        "size":              int(slot.get("sizeId")) if slot.get("sizeId") else None,
        "cupSize":           slot.get("cupSizeId"),
        "availableQuantity": slot["availableQuantity"],
        "mrpPrice":          slot["mrpPrice"],
        "finalPrice":        slot["finalPrice"],
        "discountPercent":   slot.get("discountPercent") or 0,
        # FIX #7 — stock flags correctly derived
        "isLowStock":        slot.get("isLowStock", False),
        "isOutOfStock":      slot.get("isOutOfStock", False),
        "now":               NOW(),
    })
    return r.scalar_one()


# ══════════════════════════════════════════════════════════════════════════════
#  ROUTES — Generic entities
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/entities", summary="List all supported bulk-import entities")
def list_entities():
    return {
        "entities": [
            {
                "key":      k,
                "label":    k.replace("_", " ").title(),
                "columns":  [c[1] for c in v["columns"]],
                "required": v["required"],
                "notes":    v["notes"],
            }
            for k, v in ENTITY_CONFIG.items()
        ]
    }


@router.get("/template/{entity}", summary="Download Excel template for a generic entity")
def download_entity_template(entity: str):
    _check_openpyxl()
    if entity not in ENTITY_CONFIG:
        raise HTTPException(status_code=404, detail=f"Entity '{entity}' not supported.")

    cfg      = ENTITY_CONFIG[entity]
    col_defs = cfg["columns"]
    sample   = cfg.get("sample", {})

    BRAND_RED  = "6B0F2A"
    LIGHT_PINK = "F5E6EA"

    wb = Workbook()
    ws = wb.active
    ws.title = entity.title()

    border = _thin_border()
    center = _center_align()
    hdr_font = Font(bold=True, color="FFFFFF", size=11)
    hdr_fill = PatternFill("solid", fgColor=BRAND_RED)
    req_fill = PatternFill("solid", fgColor=LIGHT_PINK)

    for col_idx, (field, label, dtype, req) in enumerate(col_defs, start=1):
        c = ws.cell(row=1, column=col_idx, value=label)
        c.font = hdr_font
        c.fill = hdr_fill
        c.alignment = center
        c.border = border
        ws.column_dimensions[get_column_letter(col_idx)].width = max(20, len(label) + 4)

    for col_idx, (field, label, dtype, req) in enumerate(col_defs, start=1):
        c = ws.cell(row=2, column=col_idx, value=sample.get(field, ""))
        c.fill = req_fill if req else PatternFill("solid", fgColor="FFFFFF")
        c.border = border

    note_font = Font(italic=True, color="666666", size=9)
    ws.cell(row=4, column=1, value="Notes:").font = Font(bold=True, size=10)
    for i, note in enumerate([
        cfg["notes"],
        "Columns marked with * are required.",
        "Delete row 2 (sample) before uploading.",
    ], start=5):
        ws.cell(row=i, column=1, value=f"• {note}").font = note_font

    ws.freeze_panes = "A2"
    ws.row_dimensions[1].height = 30

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="twam_{entity}_template.xlsx"'},
    )


@router.post("/preview/{entity}", summary="Preview generic entity upload — no DB write")
async def preview_entity(
    entity: str,
    file: UploadFile = File(...),
):
    if entity not in ENTITY_CONFIG:
        raise HTTPException(status_code=404, detail=f"Entity '{entity}' not supported.")

    content = await file.read()
    rows    = _parse_entity_excel(content, entity)

    return {
        "entity":      entity,
        "totalRows":   len(rows),
        "validRows":   sum(1 for r in rows if not r["_errors"]),
        "invalidRows": sum(1 for r in rows if r["_errors"]),
        "columns":     [c[1] for c in ENTITY_CONFIG[entity]["columns"]],
        "rows":        rows[:200],
        "canImport":   any(not r["_errors"] for r in rows),
    }


@router.post("/import/{entity}", summary="Import generic entity rows — skips invalid rows")
async def import_entity(
    entity: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    if entity not in ENTITY_CONFIG:
        raise HTTPException(status_code=404, detail=f"Entity '{entity}' not supported.")
    if current_user.role_id not in {Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER, Roles.INVENTORY_MANAGER}:
        raise HTTPException(status_code=403, detail="Insufficient permissions.")

    content  = await file.read()
    rows     = _parse_entity_excel(content, entity)
    now      = NOW()
    inserted = skipped = 0
    row_errors: list[dict] = []

    for row in rows:
        if row["_errors"]:
            skipped += 1
            row_errors.append({"row": row["_row"], "errors": row["_errors"]})
            continue
        try:
            _insert_entity_row(db, entity, row, current_user.user_id, now)
            inserted += 1
        except Exception as exc:
            db.rollback()
            skipped += 1
            row_errors.append({"row": row["_row"], "errors": [str(exc)]})

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Commit failed: {exc}")

    return {
        "entity":   entity,
        "inserted": inserted,
        "skipped":  skipped,
        "errors":   row_errors[:50],
        "message":  f"Successfully imported {inserted} {entity}. {skipped} row(s) skipped.",
    }


# ══════════════════════════════════════════════════════════════════════════════
#  HIERARCHICAL EXCEL FORMAT
#  ─────────────────────────
#  Column layout (fixed, left-to-right):
#    A  Row Type      → "PRODUCT" | "VARIANT" | "SIZE"
#
#  Product columns (B–G):
#    B  Product Code*
#    C  Product Name*
#    D  Description*
#    E  Category ID*
#    F  Brand ID*
#    G  Child Cat ID
#
#  Variant columns (H–P):
#    H  Product Code*      ← renamed (was Variant Code)
#    I  Variant Name*
#    J  Variant Description
#    K  Fabric ID
#    L  Color
#    M  Is Best Seller
#    N  Is Return Available
#    O  Is Cup Size*        ← mandatory
#    P  Variant TAX HSN Code
#
#  Size columns (Q–V):
#    Q  Size ID*
#    R  Cup Size ID         ← moved next to Size ID
#    S  MRP Price*
#    T  Final Price*
#    U  Stock Qty*
#    V  Discount %
#
#  REMOVED columns: Stock No, Personal ID, User Profile ID, Created By,
#                   Stock ID, CGST, SGST, Tax Amount, Is Alphabet Size
# ══════════════════════════════════════════════════════════════════════════════

_COL_ROW_TYPE = 0   # A

def _to_bool(val):
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().upper() in ("TRUE", "1", "YES", "Y")
    return False

# FIX #12 — column indices realigned; no gaps between product / variant / size blocks
# Product field positions (B=1 … G=6)
_P_FIELDS = [
    ("productCode",     1, "str", True),   # B
    ("name",            2, "str", True),   # C
    ("description",     3, "str", True),   # D
    ("categoryId",      4, "int", True),   # E
    ("brandId",         5, "int", True),   # F
    ("childCategoryId", 6, "int", False),  # G
]

# Variant field positions (H=7 … P=15)
_V_FIELDS = [
    ("variantCode",         7,  "str",  True),  # H — Product Code (renamed)
    ("variantName",         8,  "str",  True),  # I
    ("variantDescription",  9,  "str",  False), # J
    ("fabricId",            10, "int",  False), # K
    ("color",               11, "str",  False), # L
    ("isBestSeller",        12, "bool", False), # M
    ("isReturnAvailable",   13, "bool", False), # N
    # FIX #4 — isCupSize is required (req=True)
    ("isCupSize",           14, "bool", True),  # O
    ("variantTaxHSNCodeId", 15, "int",  False), # P
]

# Size field positions (Q=16 … V=21)
_S_FIELDS = [
    ("sizeId",            16, "int",   True),  # Q
    ("cupSizeId",         17, "int",   False), # R  ← next to sizeId
    ("mrpPrice",          18, "float", True),  # S
    ("finalPrice",        19, "float", True),  # T
    ("availableQuantity", 20, "int",   True),  # U
    ("discountPercent",   21, "int",   False), # V
]

_TOTAL_COLS = 22  # A–V (0-based max index 21, so 22 columns)


def _cell(row: tuple, col: int) -> Any:
    return row[col] if col < len(row) else None


# ── Hierarchical template builder ─────────────────────────────────────────────

def _build_hierarchical_template() -> bytes:
    """
    Build the self-guiding grouped Excel template.
    Fields removed: Stock No, Personal ID, User Profile ID, Created By,
                    Stock ID, CGST, SGST, Tax Amount, Is Alphabet Size.
    Renamed: TaxHSN Code ID → TAX HSN Code, Variant Code → Product Code.
    Size ID and Cup Size ID are now side by side.
    isCupSize is now marked required (*).
    """
    _check_openpyxl()

    C_PROD_HDR = "6B0F2A"
    C_VAR_HDR  = "1A3A5C"
    C_SIZE_HDR = "14532D"
    C_TYPE_HDR = "2D2D2D"
    C_PROD_ROW = "FFF0F3"
    C_VAR_ROW  = "EFF6FF"
    C_SIZE_ROW = "F0FDF4"
    C_PROD_TYP = "6B0F2A"
    C_VAR_TYP  = "1D4ED8"
    C_SIZE_TYP = "047857"
    C_BLANK    = "F5F5F5"
    C_SEP      = "EFEFEF"
    C_GUIDE_BG = "FFFBF0"
    C_GUIDE_BD = "F0D9A0"

    wb = Workbook()
    ws = wb.active
    ws.title = "Product Bulk Upload"

    thin = _thin_border()
    ctr  = _center_align()

    def hdr_cell(col0, label, bg, width=18):
        c = ws.cell(row=1, column=col0 + 1, value=label)
        c.font      = Font(bold=True, color="FFFFFF", size=10)
        c.fill      = PatternFill("solid", fgColor=bg)
        c.alignment = ctr
        c.border    = thin
        ws.column_dimensions[get_column_letter(col0 + 1)].width = max(width, len(label) + 4)

    def data_cell(row_n, col0, value, bg, bold=False, color="1A1917", indent=0):
        c = ws.cell(row=row_n, column=col0 + 1, value=value)
        c.fill      = PatternFill("solid", fgColor=bg)
        c.font      = Font(bold=bold, color=color, size=10)
        c.border    = thin
        c.alignment = Alignment(horizontal="left", vertical="center", indent=indent)
        return c

    def blank_row(row_n, bg=C_SEP):
        for col_i in range(_TOTAL_COLS):
            c = ws.cell(row=row_n, column=col_i + 1, value="")
            c.fill   = PatternFill("solid", fgColor=bg)
            c.border = thin

    # ── header row ─────────────────────────────────────────────────────────
    hdr_cell(_COL_ROW_TYPE, "Row Type ▼", C_TYPE_HDR, width=14)

    # Product headers
    _P_LABELS = {
        "productCode":     "Product Code*",
        "name":            "Product Name*",
        "description":     "Description*",
        "categoryId":      "Category ID*",
        "brandId":         "Brand ID*",
        "childCategoryId": "Child Category ID",
    }
    for field, col, dtype, req in _P_FIELDS:
        hdr_cell(col, _P_LABELS[field], C_PROD_HDR)

    # Variant headers
    _V_LABELS = {
        "variantCode":         "Product Code*",       # renamed
        "variantName":         "Variant Name*",
        "variantDescription":  "Variant Description",
        "fabricId":            "Fabric ID",
        "color":               "Color",
        "isBestSeller":        "Is Best Seller",
        "isReturnAvailable":   "Is Return Available",
        # FIX #4 — marked required with *
        "isCupSize":           "Is Cup Size*",
        "variantTaxHSNCodeId": "Variant TAX HSN Code",
    }
    for field, col, dtype, req in _V_FIELDS:
        hdr_cell(col, _V_LABELS[field], C_VAR_HDR)

    # Size headers — Size ID and Cup Size ID side by side
    _S_LABELS = {
        "sizeId":            "Size ID*",
        "cupSizeId":         "Cup Size ID",
        "mrpPrice":          "MRP Price*",
        "finalPrice":        "Final Price*",
        "availableQuantity": "Stock Qty*",
        "discountPercent":   "Discount %",
    }
    for field, col, dtype, req in _S_FIELDS:
        hdr_cell(col, _S_LABELS[field], C_SIZE_HDR)

    ws.row_dimensions[1].height = 34
    ws.freeze_panes = "A2"

    # ── Row Type dropdown ──────────────────────────────────────────────────
    dv_type = DataValidation(
        type="list",
        formula1='"PRODUCT,VARIANT,SIZE"',
        allow_blank=True,
        showDropDown=False,
        showErrorMessage=True,
        errorTitle="Invalid Row Type",
        error="Please choose PRODUCT, VARIANT, or SIZE from the dropdown.",
        showInputMessage=True,
        promptTitle="Row Type",
        prompt=(
            "PRODUCT — one row per product (fill red columns).\n"
            "VARIANT — one row per colour/style (fill blue columns).\n"
            "SIZE    — one row per size entry (fill green columns)."
        ),
    )
    ws.add_data_validation(dv_type)
    dv_type.sqref = "A2:A5000"

    # ── Boolean dropdowns for variant & size bool fields ───────────────────
    for field, col, dtype, req in list(_V_FIELDS) + list(_S_FIELDS):
        if dtype == "bool":
            col_letter = get_column_letter(col + 1)
            bv = DataValidation(
                type="list",
                formula1='"TRUE,FALSE"',
                allow_blank=True,
                showDropDown=False,
                showErrorMessage=True,
                errorTitle="Boolean field",
                error="Please enter TRUE or FALSE.",
            )
            ws.add_data_validation(bv)
            bv.sqref = f"{col_letter}2:{col_letter}5000"

    # ── Conditional formatting ─────────────────────────────────────────────
    data_range = f"A2:{get_column_letter(_TOTAL_COLS)}5000"
    for keyword, fill_hex, font_hex in [
        ("PRODUCT", C_PROD_ROW, C_PROD_TYP),
        ("VARIANT", C_VAR_ROW,  C_VAR_TYP),
        ("SIZE",    C_SIZE_ROW, C_SIZE_TYP),
    ]:
        rule = FormulaRule(
            formula=[f'UPPER(TRIM($A2))="{keyword}"'],
            fill=PatternFill("solid", fgColor=fill_hex),
            font=Font(color=font_hex),
            stopIfTrue=False,
        )
        ws.conditional_formatting.add(data_range, rule)

    # ── Sample data ────────────────────────────────────────────────────────
    sample_products = [
        {
            "productCode": "PRD-001", "name": "Product 1",
            "description": "First product description",
            "categoryId": 1, "brandId": 1, "childCategoryId": "",
        },
        {
            "productCode": "PRD-002", "name": "Product 2",
            "description": "Second product description",
            "categoryId": 2, "brandId": 1, "childCategoryId": "",
        },
    ]
    sample_variants = [
        {"variantCode": "VAR-RED",   "variantName": "Red Variant",   "color": "Red",
         "isBestSeller": "TRUE",  "isReturnAvailable": "TRUE",  "isCupSize": "FALSE",
         "variantDescription": "", "fabricId": "", "variantTaxHSNCodeId": ""},
        {"variantCode": "VAR-BLUE",  "variantName": "Blue Variant",  "color": "Blue",
         "isBestSeller": "FALSE", "isReturnAvailable": "TRUE",  "isCupSize": "FALSE",
         "variantDescription": "", "fabricId": "", "variantTaxHSNCodeId": ""},
        {"variantCode": "VAR-GREEN", "variantName": "Green Variant", "color": "Green",
         "isBestSeller": "FALSE", "isReturnAvailable": "FALSE", "isCupSize": "FALSE",
         "variantDescription": "", "fabricId": "", "variantTaxHSNCodeId": ""},
    ]
    sample_sizes = [
        {"sizeId": 1, "cupSizeId": "", "mrpPrice": 999, "finalPrice": 799,
         "availableQuantity": 50, "discountPercent": 20},
        {"sizeId": 2, "cupSizeId": "", "mrpPrice": 999, "finalPrice": 799,
         "availableQuantity": 30, "discountPercent": 20},
    ]

    structure = [
        (sample_products[0], [sample_variants[0], sample_variants[1]], sample_sizes),
        (sample_products[1], [sample_variants[2]], sample_sizes),
    ]

    row_n = 2
    for prod, variants, sizes in structure:
        # PRODUCT row
        data_cell(row_n, _COL_ROW_TYPE, "PRODUCT", C_PROD_ROW, bold=True, color=C_PROD_TYP)
        for field, col, dtype, req in _P_FIELDS:
            data_cell(row_n, col, prod.get(field, ""), C_PROD_ROW)
        for field, col, dtype, req in list(_V_FIELDS) + list(_S_FIELDS):
            data_cell(row_n, col, "", C_BLANK)
        ws.row_dimensions[row_n].height = 20
        row_n += 1

        for var in variants:
            # VARIANT row
            data_cell(row_n, _COL_ROW_TYPE, "VARIANT", C_VAR_ROW, bold=True, color=C_VAR_TYP)
            for field, col, dtype, req in _P_FIELDS:
                data_cell(row_n, col, "", C_BLANK)
            for field, col, dtype, req in _V_FIELDS:
                data_cell(row_n, col, var.get(field, ""), C_VAR_ROW, indent=1)
            for field, col, dtype, req in _S_FIELDS:
                data_cell(row_n, col, "", C_BLANK)
            ws.row_dimensions[row_n].height = 20
            row_n += 1

            for sz in sizes:
                # SIZE row
                data_cell(row_n, _COL_ROW_TYPE, "SIZE", C_SIZE_ROW, color=C_SIZE_TYP)
                for field, col, dtype, req in list(_P_FIELDS) + list(_V_FIELDS):
                    data_cell(row_n, col, "", C_BLANK)
                for field, col, dtype, req in _S_FIELDS:
                    data_cell(row_n, col, sz.get(field, ""), C_SIZE_ROW, indent=2)
                ws.row_dimensions[row_n].height = 18
                row_n += 1

        blank_row(row_n)
        ws.row_dimensions[row_n].height = 6
        row_n += 1

    # ── Amber guide banner ─────────────────────────────────────────────────
    guide_border = Border(
        left=Side(style="medium", color=C_GUIDE_BD),
        right=Side(style="medium", color=C_GUIDE_BD),
        top=Side(style="medium", color=C_GUIDE_BD),
        bottom=Side(style="medium", color=C_GUIDE_BD),
    )
    for col_i in range(_TOTAL_COLS):
        val = (
            "👇  Your data goes below — "
            "select a Row Type from the dropdown in column A to start  👇"
            if col_i == 0 else ""
        )
        c = ws.cell(row=row_n, column=col_i + 1, value=val)
        c.fill      = PatternFill("solid", fgColor=C_GUIDE_BG)
        c.font      = Font(bold=True, color="92400E", size=10, italic=True)
        c.border    = guide_border
        c.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[row_n].height = 22
    row_n += 1

    # ── 3 pre-typed starter rows ───────────────────────────────────────────
    for rtype, bg, fg in [
        ("PRODUCT", C_PROD_ROW, C_PROD_TYP),
        ("VARIANT", C_VAR_ROW,  C_VAR_TYP),
        ("SIZE",    C_SIZE_ROW, C_SIZE_TYP),
    ]:
        data_cell(row_n, _COL_ROW_TYPE, rtype, bg, bold=True, color=fg)
        for col_i in range(1, _TOTAL_COLS):
            c = ws.cell(row=row_n, column=col_i + 1, value="")
            c.fill   = PatternFill("solid", fgColor=bg)
            c.border = thin
        ws.row_dimensions[row_n].height = 20
        row_n += 1

    # ── 10 blank rows ──────────────────────────────────────────────────────
    for _ in range(10):
        for col_i in range(_TOTAL_COLS):
            ws.cell(row=row_n, column=col_i + 1, value="").border = thin
        ws.row_dimensions[row_n].height = 18
        row_n += 1

    # ── In-sheet legend ────────────────────────────────────────────────────
    leg_start = row_n + 1
    legend_items = [
        ("PRODUCT", C_PROD_ROW, C_PROD_TYP,
         "One PRODUCT row per product. Fill columns B–G (red header columns). "
         "Required: Product Code, Product Name, Description, Category ID, Brand ID."),
        ("VARIANT", C_VAR_ROW, C_VAR_TYP,
         "One VARIANT row per colour/style, placed directly below its PRODUCT row. "
         "Fill columns H–P (blue columns). "
         "Required: Product Code (col H), Variant Name, Is Cup Size*."),
        ("SIZE", C_SIZE_ROW, C_SIZE_TYP,
         "One SIZE row per size option, placed directly below its VARIANT row. "
         "Fill columns Q–V (green columns). Required: Size ID, MRP Price, Final Price, Stock Qty. "
         "Cup Size ID (col R) is optional — only fill when Is Cup Size = TRUE on the VARIANT row. "
         "Final Price must be ≤ MRP Price."),
    ]

    for col_i in range(_TOTAL_COLS):
        val = (
            "📋  Row Type Legend — use the Column A dropdown for every new row you add"
            if col_i == 0 else ""
        )
        c = ws.cell(row=leg_start, column=col_i + 1, value=val)
        c.fill      = PatternFill("solid", fgColor="2D2D2D")
        c.font      = Font(bold=True, color="FFFFFF", size=11)
        c.alignment = Alignment(horizontal="left", vertical="center")
        c.border    = thin
    ws.row_dimensions[leg_start].height = 28
    leg_row = leg_start + 1

    for rtype, bg, fg, desc in legend_items:
        ca = ws.cell(row=leg_row, column=1, value=rtype)
        ca.fill      = PatternFill("solid", fgColor=bg)
        ca.font      = Font(bold=True, color=fg, size=10)
        ca.alignment = ctr
        ca.border    = thin
        cb = ws.cell(row=leg_row, column=2, value=desc)
        cb.fill      = PatternFill("solid", fgColor="FAFAFA")
        cb.font      = Font(size=10)
        cb.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
        cb.border    = thin
        ws.merge_cells(
            start_row=leg_row, start_column=2,
            end_row=leg_row,   end_column=_TOTAL_COLS,
        )
        ws.row_dimensions[leg_row].height = 40
        leg_row += 1

    # ── "📋 How to Fill" notes sheet ───────────────────────────────────────
    ws_n = wb.create_sheet("📋 How to Fill")
    ws_n.column_dimensions["A"].width = 26
    ws_n.column_dimensions["B"].width = 70

    notes = [
        ("Row Type dropdown (col A)",
         "Click any cell in column A (row 2 onwards) and pick from the dropdown: "
         "PRODUCT, VARIANT, or SIZE. The row auto-colours once you select a type."),
        ("Adding a new product",
         "1. Find a blank row.  2. Pick PRODUCT from the col A dropdown.  "
         "3. Fill columns B–G (red header columns).  Leave H–V blank."),
        ("Adding a variant",
         "Directly below the PRODUCT row, pick VARIANT. Fill columns H–P (blue headers). "
         "Is Cup Size* (col O) is mandatory — always set TRUE or FALSE."),
        ("Adding a size",
         "Directly below the VARIANT row, pick SIZE. Fill columns Q–V (green headers). "
         "Size ID (col Q) and Cup Size ID (col R) are side by side. "
         "MRP Price, Final Price, and Stock Qty are required."),
        ("Cup Size rules",
         "If Is Cup Size = TRUE on a VARIANT row, every SIZE row below it must include a Cup Size ID. "
         "If Is Cup Size = FALSE, the Cup Size ID column must be left blank."),
        ("Hierarchy rule",
         "The parser reads rows top-to-bottom. A VARIANT belongs to the last PRODUCT row above it. "
         "A SIZE belongs to the last VARIANT row above it. Order is critical."),
        ("Required fields (*)",
         "PRODUCT: Product Code, Product Name, Description, Category ID, Brand ID.\n"
         "VARIANT: Product Code (col H), Variant Name, Is Cup Size.\n"
         "SIZE: Size ID, MRP Price, Final Price, Stock Qty."),
        ("Final Price rule",
         "Final Price must be ≤ MRP Price. Rows that break this rule are flagged in "
         "preview and skipped during import."),
        ("Boolean fields",
         "Use the cell dropdown to pick TRUE or FALSE. "
         "Leaving a boolean cell blank is treated as FALSE except for Is Cup Size, "
         "which is required and will raise a validation error if left blank."),
        ("Stock flags (auto-computed)",
         "isOutOfStock is set automatically when Stock Qty = 0. "
         "isLowStock is set when Stock Qty > 0 and < 10. "
         "You do not need to fill these columns — they are derived on import."),
        ("Removed fields",
         "The following fields have been removed from this template: "
         "Stock No, Personal ID, User Profile ID, Created By, "
         "Stock ID, CGST, SGST, Tax Amount, Is Alphabet Size."),
        ("Renamed fields",
         "'TAX HSN Code' replaces 'TaxHSN Code ID' for variant rows. "
         "'Product Code' (col H) replaces 'Variant Code' — fill in the variant's unique code here."),
        ("Import behaviour",
         "Rows with errors are skipped. Variants with no valid sizes are skipped. "
         "Products with no valid variants are skipped. All valid records import in one "
         "DB transaction (full rollback on error)."),
        ("Preview first",
         "Always run the Preview step in the web UI before importing — it shows which rows "
         "will be accepted and which will be skipped, with reasons."),
    ]

    for col_i, val in enumerate(["Topic", "Guidance"], start=1):
        hc = ws_n.cell(row=1, column=col_i, value=val)
        hc.font = Font(bold=True, color="FFFFFF", size=11)
        hc.fill = PatternFill("solid", fgColor="2D2D2D")
        hc.border = _thin_border()
        hc.alignment = _center_align()
    ws_n.row_dimensions[1].height = 26

    for i, (topic, guidance) in enumerate(notes, start=2):
        ca = ws_n.cell(row=i, column=1, value=topic)
        cb = ws_n.cell(row=i, column=2, value=guidance)
        ca.font = Font(bold=True, size=10)
        cb.font = Font(size=10)
        cb.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
        for c in (ca, cb):
            c.border = _thin_border()
            c.fill   = PatternFill("solid", fgColor="F8F8F8" if i % 2 == 0 else "FFFFFF")
        ws_n.row_dimensions[i].height = 42

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


# ── Hierarchical Excel parser ──────────────────────────────────────────────────

def _parse_hierarchical_excel(file_bytes: bytes) -> list[dict]:
    """
    Read the hierarchical sheet.
    Returns a flat list of fully-resolved SIZE records, each carrying
    the product + variant context they belong to, plus _errors and _valid.
    """
    _check_openpyxl()
    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Cannot read Excel file: {exc}")

    ws   = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise HTTPException(status_code=400, detail="The Excel file is empty.")

    current_product: dict  = {}
    current_variant: dict  = {}
    size_records: list[dict] = []

    for row_num, row in enumerate(rows[1:], start=2):
        if not row or len(row) == 0:
            continue

        raw_type = str(_cell(row, _COL_ROW_TYPE) or "").strip().upper().lstrip()
        if not raw_type or raw_type in ("", "ROW TYPE ▼"):
            continue

        # ── PRODUCT row ────────────────────────────────────────────────────
        if "PRODUCT" in raw_type:
            current_product = {"_row": row_num}
            current_variant = {}
            for field, col, dtype, req in _P_FIELDS:
                try:
                    current_product[field] = _cast(_cell(row, col), dtype)
                except ValueError:
                    current_product[field] = None

        # ── VARIANT row ────────────────────────────────────────────────────
        elif "VARIANT" in raw_type:
            current_variant = {"_row": row_num}
            for field, col, dtype, req in _V_FIELDS:
                try:
                    current_variant[field] = _cast(_cell(row, col), dtype)
                except ValueError:
                    current_variant[field] = None

        # ── SIZE row ───────────────────────────────────────────────────────
        elif "SIZE" in raw_type:
            # FIX #2 — skip template sample rows
            if _is_sample_row({
                "productCode": current_product.get("productCode"),
                "variantCode": current_variant.get("variantCode"),
                "name":        current_product.get("name"),
            }):
                continue
            if not current_product:
                continue
            if not current_variant:
                continue

            errors: list[str] = []
            rec: dict = {
                "_row":   row_num,
                "_p_row": current_product.get("_row"),
                "_v_row": current_variant.get("_row"),
                **current_product,
                **current_variant,
            }

            for field, col, dtype, req in _S_FIELDS:
                raw = _cell(row, col)
                try:
                    val = _cast(raw, dtype)
                except ValueError:
                    errors.append(f"Row {row_num}: Size field '{field}' has invalid value '{raw}'")
                    val = None
                if req and val is None:
                    errors.append(f"Row {row_num}: Size field '{field}' is required")
                rec[field] = val

            # validate required product fields
            for field, col, dtype, req in _P_FIELDS:
                if req and not current_product.get(field):
                    errors.append(
                        f"Row {current_product.get('_row', '?')}: Product field '{field}' is required"
                    )

            # FIX #4 — validate required variant fields including isCupSize
            for field, col, dtype, req in _V_FIELDS:
                if req and current_variant.get(field) is None:
                    errors.append(
                        f"Row {current_variant.get('_row', '?')}: Variant field '{field}' is required"
                    )

            # FIX #3 & #5 — cup size cross-validation with corrected boolean normalisation
            is_cup = current_variant.get("isCupSize")
            # Normalise: _cast returns True/False/None; treat None as False
            # This prevents false errors when isCupSize cell contains "FALSE"
            if isinstance(is_cup, str):
                is_cup = is_cup.strip().upper() == "TRUE"
            elif is_cup is None:
                is_cup = False

            cup_id = rec.get("cupSizeId")

            # Case 1: isCupSize=TRUE but Cup Size ID is missing
            if is_cup and not cup_id:
                errors.append(
                    f"Row {row_num}: Is Cup Size = TRUE but Cup Size ID is missing"
                )

            # Case 2: isCupSize=FALSE but Cup Size ID was provided
            if not is_cup and cup_id:
                errors.append(
                    f"Row {row_num}: Is Cup Size = FALSE but Cup Size ID should not be provided"
                )

            # FIX #6 — final price must not exceed MRP
            mrp   = rec.get("mrpPrice")
            final = rec.get("finalPrice")
            if mrp is not None and final is not None and final > mrp:
                errors.append(f"Row {row_num}: Final Price ({final}) cannot exceed MRP Price ({mrp})")

            # FIX #7 — derive stock flags
            stock  = rec.get("availableQuantity") or 0
            is_out = stock == 0
            is_low = not is_out and stock < 10
            rec["isOutOfStock"] = is_out
            rec["isLowStock"]   = is_low

            rec["_errors"] = errors
            rec["_valid"]  = len(errors) == 0
            size_records.append(rec)

    return size_records


def _build_hierarchical_chains(size_records: list[dict]) -> dict[str, dict]:
    """
    Build chains keyed by the PRODUCT row number (_p_row).
    All SIZE records sharing the same _p_row belong to the same product.
    All SIZE records sharing the same _p_row + _v_row belong to the same variant.
    """
    chains: dict[str, dict] = {}

    for rec in size_records:
        if not rec["_valid"]:
            continue
        # FIX #2 — skip sample rows in chain builder too
        if _is_sample_row(rec):
            continue

        pcode = rec.get("productCode")
        vcode = rec.get("variantCode")
        if not pcode or not vcode:
            continue

        p_row = rec.get("_p_row")
        v_row = rec.get("_v_row")

        pkey = f"p_row_{p_row}_{pcode}"
        vkey = f"v_row_{v_row}_{vcode}"

        if pkey not in chains:
            chains[pkey] = {"rec": rec, "variants": {}}

        if vkey not in chains[pkey]["variants"]:
            chains[pkey]["variants"][vkey] = {"rec": rec, "slots": []}

        # FIX #7 — stock flags from the SIZE record (already computed above)
        chains[pkey]["variants"][vkey]["slots"].append({
            "sizeId":            rec["sizeId"],
            "cupSizeId":         rec.get("cupSizeId"),
            "mrpPrice":          rec["mrpPrice"],
            "finalPrice":        rec["finalPrice"],
            "availableQuantity": rec["availableQuantity"],
            "discountPercent":   rec.get("discountPercent") or 0,
            "isLowStock":        rec.get("isLowStock", False),
            "isOutOfStock":      rec.get("isOutOfStock", False),
        })

    # Clean up variants with no slots
    result = {}
    for pkey, chain in chains.items():
        variant_list = [v for v in chain["variants"].values() if v["slots"]]
        if variant_list:
            result[pkey] = {"rec": chain["rec"], "variants": variant_list}

    return result


# ══════════════════════════════════════════════════════════════════════════════
#  ROUTES — Product Bulk  (/product-bulk/*)
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/product-bulk/template",
    summary="Download hierarchical Excel template (Product → Variant → Size grouped)",
)
def download_product_bulk_template():
    xlsx_bytes = _build_hierarchical_template()
    return StreamingResponse(
        io.BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="twam_product_bulk_template.xlsx"'},
    )


@router.post(
    "/product-bulk/preview",
    summary="Preview hierarchical product bulk file — all SIZE rows with OK/Error status, no DB write",
)
async def preview_product_bulk(file: UploadFile = File(...)):
    content      = await file.read()
    size_records = _parse_hierarchical_excel(content)
    chains       = _build_hierarchical_chains(size_records)

    preview_rows = []
    for rec in size_records:
        preview_rows.append({
            "row":         rec["_row"],
            "status":      "OK" if rec["_valid"] else "Error",
            "issues":      rec["_errors"],
            "productCode": rec.get("productCode"),
            "productName": rec.get("name"),
            "variantCode": rec.get("variantCode"),
            "variantName": rec.get("variantName"),
            "color":       rec.get("color"),
            "sizeId":      rec.get("sizeId"),
            "cupSizeId":   rec.get("cupSizeId"),
            "mrpPrice":    rec.get("mrpPrice"),
            "finalPrice":  rec.get("finalPrice"),
            "stockQty":    rec.get("availableQuantity"),
            # FIX #7 — expose stock flags in preview response
            "isLowStock":  rec.get("isLowStock", False),
            "isOutOfStock": rec.get("isOutOfStock", False),
        })

    return {
        "totalSizeRows":      len(size_records),
        "validRows":          sum(1 for r in size_records if r["_valid"]),
        "errorRows":          sum(1 for r in size_records if not r["_valid"]),
        "willImportProducts": len(chains),
        "willImportVariants": sum(len(v["variants"]) for v in chains.values()),
        "willImportDetails":  sum(
            len(var["slots"])
            for v in chains.values()
            for var in v["variants"]
        ),
        "canImport": len(chains) > 0,
        "rows":      preview_rows[:500],
    }


@router.post(
    "/product-bulk/import",
    summary="Execute hierarchical product bulk import — full rollback on DB error",
)
async def import_product_bulk(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    # FIX #14 — role-based permission check
    if current_user.role_id not in {Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER, Roles.INVENTORY_MANAGER}:
        raise HTTPException(status_code=403, detail="Insufficient permissions.")

    content      = await file.read()
    size_records = _parse_hierarchical_excel(content)
    chains       = _build_hierarchical_chains(size_records)

    skipped_rows = sum(1 for r in size_records if not r["_valid"])

    if not chains:
        return {
            "message":          "No valid rows to import after filtering out errors.",
            "insertedProducts": 0,
            "insertedVariants": 0,
            "insertedDetails":  0,
            "skippedRows":      skipped_rows,
        }

    inserted_products = inserted_variants = inserted_details = 0

    try:
        for pcode, chain in chains.items():
            p_rec      = chain["rec"]
            product_id = _pb_insert_product(db, p_rec)
            inserted_products += 1

            for var in chain["variants"]:
                v_rec      = var["rec"]
                variant_id = _pb_insert_variant(db, v_rec, product_id)
                inserted_variants += 1

                for slot in var["slots"]:
                    _pb_insert_detail(
                        db, slot,
                        product_id, variant_id,
                        v_rec["variantCode"],
                    )
                    inserted_details += 1

        # FIX #11 — commit only after all inserts succeed
        db.commit()

    except Exception as exc:
        # FIX #11 — full rollback on any DB error
        db.rollback()
        logger.exception("Product bulk import failed — full rollback: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Import aborted and fully rolled back due to a DB error: {exc}",
        )

    return {
        "message": (
            f"Successfully imported {inserted_products} product(s), "
            f"{inserted_variants} variant(s), {inserted_details} size detail(s). "
            f"{skipped_rows} row(s) skipped due to validation errors."
        ),
        "insertedProducts": inserted_products,
        "insertedVariants": inserted_variants,
        "insertedDetails":  inserted_details,
        "skippedRows":      skipped_rows,
    }