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
HSN lookup
───────────────────────────────────────────────────────────────────────────────
  GET  /api/BulkImport/hsn-tax-info                    CGST/SGST for HSN codes

───────────────────────────────────────────────────────────────────────────────
Product Bulk  (Products + ProductVariants + ProductVariantDetail, hierarchical)
───────────────────────────────────────────────────────────────────────────────
  GET  /api/BulkImport/product-bulk/template           download hierarchical template
  POST /api/BulkImport/product-bulk/preview            validate all rows + HSN warnings
  POST /api/BulkImport/product-bulk/import             insert (full rollback on DB error)

Column layout (fixed, left-to-right):
  A   Row Type        -> "PRODUCT" | "VARIANT" | "SIZE"

  Product columns (B-G):
    B  Product Code*   C  Product Name*   D  Description*
    E  Category*       F  Child Category  G  Brand*

  Variant columns (H-P):
    H  Product Code*        I  Variant Name*     J  Variant Description
    K  Fabric* (required)   L  Color* (required) M  Is Best Seller
    N  Is Return Available  O  Is Cup Size*      P  TAX HSN Code

  Size columns (Q-AC):
    Q  Stock ID          R  Size ID*         S  Cup Size ID
    T  Purchase Price*   U  MRP Price*       V  Final Price*
    W  Discount %        X  Stock Qty*       Y  CGST % (auto, locked)
    Z  SGST % (auto, locked)  AA  Store Name   AB  Warehouse Name
    AC  Date*

  NOTE: Either Store Name (AA) or Warehouse Name (AB) must be provided.
        Date (AC) is required and stored in ProductVariantDetail.

DB migration (run once before deploying):
  ALTER TABLE twam."ProductVariantDetail"
    ADD COLUMN IF NOT EXISTS "PurchasePrice"  integer,
    ADD COLUMN IF NOT EXISTS "CGST"           numeric(5,2),
    ADD COLUMN IF NOT EXISTS "SGST"           numeric(5,2),
    ADD COLUMN IF NOT EXISTS "StoreId"        bigint,
    ADD COLUMN IF NOT EXISTS "WarehouseId"    bigint,
    ADD COLUMN IF NOT EXISTS "Date"           date;
"""

import io
import logging
from datetime import datetime, timezone, timedelta, date
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
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

# ==============================================================================
#  Router
# ==============================================================================

router = APIRouter(
    prefix="/api/BulkImport",
    tags=["Bulk Import"],
    dependencies=[Depends(get_current_user)],
)

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731

# ==============================================================================
#  Shared helpers
# ==============================================================================

def _check_openpyxl():
    if not OPENPYXL_AVAILABLE:
        raise HTTPException(
            status_code=500,
            detail="openpyxl is not installed. Run: pip install openpyxl",
        )


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
            return None
        if dtype == "date":
            # Handle Excel serial date numbers
            if isinstance(value, (int, float)):
                d = datetime(1899, 12, 30) + timedelta(days=int(value))
                return d.date()
            # Handle datetime objects from openpyxl
            if isinstance(value, datetime):
                return value.date()
            if isinstance(value, date):
                return value
            # Handle string dates in various formats
            for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d"):
                try:
                    return datetime.strptime(raw, fmt).date()
                except ValueError:
                    continue
            raise ValueError(f"Cannot parse date '{raw}'")
        return raw
    except (ValueError, TypeError):
        raise ValueError(f"Cannot cast '{value}' to {dtype}")


def _thin_border() -> Border:
    s = Side(style="thin", color="DDDDDD")
    return Border(left=s, right=s, top=s, bottom=s)


def _center_align() -> Alignment:
    return Alignment(horizontal="center", vertical="center", wrap_text=True)


# ==============================================================================
#  Generic entity config
# ==============================================================================

ENTITY_CONFIG: dict[str, dict] = {
    "Products": {
        "columns": [
            ("productCode",     "Product Code*",     "str",   True),
            ("name",            "Product Name*",     "str",   True),
            ("description",     "Description*",      "str",   True),
            ("categoryId",      "Category ID*",      "int",   True),
            ("brandId",         "Brand ID*",         "int",   True),
            ("childCategoryId", "Child Category ID", "int",   False),
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
               "StartDate","EndDate","IsActive","UserProfileId",
               "CreatedDate","ModifiedDate","DeletedInd")
            VALUES
              (:CouponCode,:CouponName,:Description,:Discount,
               :StartDate,:EndDate,true,:userId,
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


# ==============================================================================
#  HSN tax lookup helper
# ==============================================================================

async def _fetch_hsn_rates(db: Session, hsn_ids: list[int]) -> dict[int, dict]:
    """
    Query DB for HSN tax rates.
    """
    if not hsn_ids:
        return {}
    result = db.execute(
        text("""
            SELECT "TaxHSNCodeId", "CGST", "SGST"
            FROM   mdm."TaxHSNCode"
            WHERE  "TaxHSNCodeId" = ANY(:ids)
              AND  "IsActive" = true
              AND  "DeletedInd" = false
        """),
        {"ids": hsn_ids},
    )
    return {
        row.TaxHSNCodeId: {"cgst": float(row.CGST or 0), "sgst": float(row.SGST or 0)}
        for row in result
    }


# ==============================================================================
#  Store lookup helpers
# ==============================================================================

def _fetch_all_stores(db: Session) -> list[dict]:
    """Return all active stores as list of {storeId, storeName}."""
    result = db.execute(text("""
        SELECT "StoreId", "StoreName"
        FROM   twam."Stores"
        WHERE  "IsActive" = true
          AND  "DeletedInd" = false
        ORDER  BY "StoreName"
    """))
    return [{"storeId": row.StoreId, "storeName": row.StoreName} for row in result]


def _fetch_store_map(db: Session) -> dict[str, int]:
    """Return {storeName -> storeId} for validation during parse/import."""
    stores = _fetch_all_stores(db)
    return {s["storeName"]: s["storeId"] for s in stores}


# ==============================================================================
#  Warehouse lookup helpers  ← NEW
# ==============================================================================

def _fetch_all_warehouses(db: Session) -> list[dict]:
    """Return all active warehouses as list of {warehouseId, warehouseName}."""
    result = db.execute(text("""
        SELECT "WarehouseId", "WarehouseName"
        FROM   twam."Warehouses"
        WHERE  "IsActive" = true
        ORDER  BY "WarehouseName"
    """))
    return [{"warehouseId": row.WarehouseId, "warehouseName": row.WarehouseName}
            for row in result]


def _fetch_warehouse_map(db: Session) -> dict[str, int]:
    """Return {warehouseName -> warehouseId} for validation."""
    warehouses = _fetch_all_warehouses(db)
    return {w["warehouseName"]: w["warehouseId"] for w in warehouses}


def _fetch_ref_data(db: Session) -> dict:
    """Fetch all dropdown reference data in one call."""
    cats = db.execute(text("""
        SELECT "CategoryId", "Name", "ParentCategoryId"
        FROM   twam."Category"
        WHERE  "IsActive" = true AND "DeletedInd" = false
        ORDER  BY "Name"
    """))
    cat_rows = [{"id": r.CategoryId, "name": r.Name, "parent": r.ParentCategoryId}
                for r in cats]

    brands = db.execute(text("""
        SELECT "brandId", "brandName"
        FROM   mdm."Brand"
        WHERE  "isActive" = true AND "deletedInd" = false
        ORDER  BY "brandName"
    """))
    brand_rows = [{"id": r.brandId, "name": r.brandName} for r in brands]

    fabrics = db.execute(text("""
        SELECT "FabricId", "Name"
        FROM   mdm."Fabric"
        WHERE  "IsActive" = true AND "DeletedInd" = false
        ORDER  BY "Name"
    """))
    fabric_rows = [{"id": r.FabricId, "name": r.Name} for r in fabrics]

    hsn = db.execute(text("""
        SELECT "TaxHSNCodeId", "HSNCode", "HSNDescription", "CGST", "SGST"
        FROM   mdm."TaxHSNCode"
        WHERE  "IsActive" = true AND "DeletedInd" = false
        ORDER  BY "HSNCode"
    """))
    hsn_rows = [{"id": r.TaxHSNCodeId, "code": r.HSNCode,
                 "desc": r.HSNDescription,
                 "cgst": float(r.CGST or 0), "sgst": float(r.SGST or 0)}
                for r in hsn]


    sizes = db.execute(text("""
        SELECT "SizeId", "SizeLabel", "SizeCode"
        FROM   mdm."Size"
        WHERE  "IsActive" = true AND "DeletedInd" = false
        ORDER  BY "OrderNo", "SizeLabel"
    """))
    size_rows = [{"id": r.SizeId, "label": r.SizeLabel, "code": r.SizeCode} for r in sizes]

    cup_sizes = db.execute(text("""
        SELECT "CupSizeId", "SizeLabel"
        FROM   mdm."CupSize"
        WHERE  "IsActive" = true AND "DeletedInd" = false
        ORDER  BY "SizeLabel"
    """))
    cup_size_rows = [{"id": r.CupSizeId, "label": r.SizeLabel} for r in cup_sizes]


    return {
        "categories":      [r for r in cat_rows if not r["parent"]],
        "childCategories": [r for r in cat_rows if r["parent"]],
        "brands":          brand_rows,
        "fabrics":         fabric_rows,
        "hsnCodes":        hsn_rows,
        "sizes":           size_rows,
        "cupSizes":        cup_size_rows,
    }

# ==============================================================================
#  Hierarchical column definitions
#
#  CHANGE 1 — StockId on SIZE row (col Q, index 16)
#  CHANGE 2 — PurchasePrice on SIZE row (col T, index 19) as integer
#  CHANGE 3 — CGST/SGST auto-filled (cols Y/Z, indices 24/25); locked
#  CHANGE 4 — StoreName on SIZE row (col AA, index 26)
#  NEW       — WarehouseName on SIZE row (col AB, index 27); either Store or Warehouse required
#  NEW       — Date on SIZE row (col AC, index 28); required; stored as date
# ==============================================================================

_COL_ROW_TYPE = 0  # A

# Product field positions (B=1 ... G=6)
_P_FIELDS: list[tuple[str, int, str, bool]] = [
    ("productCode",        1, "str", True),   # B  Product Code (SKU)*
    ("name",               2, "str", True),   # C  Product Name*
    ("description",        3, "str", True),   # D  Description*
    ("categoryName",       4, "str", True),   # E  Category* (dropdown name -> ID)
    ("childCategoryName",  5, "str", False),  # F  Child Category (dropdown name -> ID)
    ("brandName",          6, "str", True),   # G  Brand* (dropdown name -> ID)
]

# Variant field positions (H=7 ... P=15)
_V_FIELDS: list[tuple[str, int, str, bool]] = [
    ("variantCode",         7,  "str",  True),  # H  Product Code*
    ("variantName",         8,  "str",  True),  # I  Variant Name*
    ("variantDescription",  9,  "str",  False), # J  Variant Description
    ("fabricName",          10, "str",  True),  # K  Fabric* (required, dropdown)
    ("color",               11, "str",  True),  # L  Color* (required)
    ("isBestSeller",        12, "bool", False), # M  Is Best Seller
    ("isReturnAvailable",   13, "bool", False), # N  Is Return Available
    ("isCupSize",           14, "bool", True),  # O  Is Cup Size*
    ("hsnCode",             15, "str",  False), # P  TAX HSN Code (dropdown)
]

# Size field positions (Q=16 ... AC=28)
# Store OR Warehouse must be provided — enforced in parser
_S_FIELDS: list[tuple[str, int, str, bool]] = [
    ("stockId",           16, "str",   False), # Q  Stock ID
    ("sizeName",            17, "str",   True),  # R  Size ID*
    ("cupSizeName",         18, "str",   False), # S  Cup Size ID
    ("purchasePrice",     19, "int",   True),  # T  Purchase Price* (required)
    ("mrpPrice",          20, "float", True),  # U  MRP Price*
    ("finalPrice",        21, "float", True),  # V  Final Price*
    ("discountPercent",   22, "int",   False), # W  Discount %
    ("availableQuantity", 23, "int",   True),  # X  Stock Qty*
    ("cgst",              24, "float", False), # Y  CGST % (auto, locked)
    ("sgst",              25, "float", False), # Z  SGST % (auto, locked)
    ("storeName",         26, "str",   False), # AA Store Name  (either this or warehouseName)
    ("warehouseName",     27, "str",   False), # AB Warehouse Name  ← NEW
    ("entryDate",         28, "date",  True),  # AC Date*  ← NEW
]

_TOTAL_COLS = 29  # A-AC


def _cell(row: tuple, col: int) -> Any:
    return row[col] if col < len(row) else None


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


# ==============================================================================
#  Hierarchical template builder
# ==============================================================================

def _build_hierarchical_template(_stores: list[dict], _refs: dict, _warehouses: list[dict]) -> bytes:
    """
    Build the Excel template with:
    - Dropdowns for Category, Child Category, Brand (PRODUCT row)
    - Dropdowns for Fabric, HSN Code (VARIANT row)
    - Dropdowns for Store and Warehouse (SIZE row) — either one must be filled
    - Date picker (calendar) for Date column (SIZE row)
    - CGST/SGST locked (auto-filled from HSN)
    """
    _check_openpyxl()

    C_PROD_HDR   = "6B0F2A"
    C_VAR_HDR    = "1A3A5C"
    C_SIZE_HDR   = "14532D"
    C_TAX_HDR    = "7C6000"   # amber for auto-tax columns
    C_STORE_HDR  = "1E3A5F"
    C_WH_HDR     = "4A1D96"   # purple for warehouse column  ← NEW
    C_DATE_HDR   = "065F46"   # teal for date column  ← NEW
    C_TYPE_HDR   = "2D2D2D"
    C_PROD_ROW   = "FFF0F3"
    C_VAR_ROW    = "EFF6FF"
    C_SIZE_ROW   = "F0FDF4"
    C_PROD_TYP   = "6B0F2A"
    C_VAR_TYP    = "1D4ED8"
    C_SIZE_TYP   = "047857"
    C_BLANK      = "F5F5F5"
    C_SEP        = "EFEFEF"
    C_LOCK_BG    = "D1D5DB"   # grey for locked CGST/SGST cells
    C_LOCK_FONT  = "6B7280"
    C_DATE_BG    = "ECFDF5"   # light teal for date cells  ← NEW

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

    # ── Header row ────────────────────────────────────────────────────────────
    hdr_cell(_COL_ROW_TYPE, "Row Type", C_TYPE_HDR, width=14)

    _P_LABELS = {
        "productCode":       "Product Code (SKU)*",
        "name":              "Product Name*",
        "description":       "Description*",
        "categoryName":      "Category*",
        "childCategoryName": "Child Category",
        "brandName":         "Brand*",
    }
    for field, col, dtype, req in _P_FIELDS:
        hdr_cell(col, _P_LABELS[field], C_PROD_HDR)

    _V_LABELS = {
        "variantCode":        "Product Code*",
        "variantName":        "Variant Name*",
        "variantDescription": "Variant Description",
        "fabricName":         "Fabric*",
        "color":              "Color*",
        "isBestSeller":       "Is Best Seller",
        "isReturnAvailable":  "Is Return Available",
        "isCupSize":          "Is Cup Size*",
        "hsnCode":            "TAX HSN Code",
    }
    for field, col, dtype, req in _V_FIELDS:
        hdr_cell(col, _V_LABELS[field], C_VAR_HDR)

    _S_LABELS = {
        "stockId":           "Stock ID",
        "sizeName":    "Size*",      
        "cupSizeName": "Cup Size",
        "purchasePrice":     "Purchase Price*",
        "mrpPrice":          "MRP Price*",
        "finalPrice":        "Final Price*",
        "discountPercent":   "Discount %",
        "availableQuantity": "Stock Qty*",
        "cgst":              "CGST % (auto)",
        "sgst":              "SGST % (auto)",
        "storeName":         "Store Name (or Warehouse)",   # updated label
        "warehouseName":     "Warehouse Name (or Store)",   # ← NEW
        "entryDate":         "Date*",                       # ← NEW
    }
    for field, col, dtype, req in _S_FIELDS:
        if field in ("cgst", "sgst"):
            bg = C_TAX_HDR
        elif field == "storeName":
            bg = C_STORE_HDR
        elif field == "warehouseName":
            bg = C_WH_HDR      # ← NEW
        elif field == "entryDate":
            bg = C_DATE_HDR    # ← NEW
        else:
            bg = C_SIZE_HDR
        hdr_cell(col, _S_LABELS[field], bg)

    ws.row_dimensions[1].height = 34
    ws.freeze_panes = "A2"

    # ── Row Type dropdown ─────────────────────────────────────────────────────
    dv_type = DataValidation(
        type="list", formula1='"PRODUCT,VARIANT,SIZE"',
        allow_blank=True, showDropDown=False,
        showErrorMessage=True, errorTitle="Invalid Row Type",
        error="Choose PRODUCT, VARIANT, or SIZE.",
    )
    ws.add_data_validation(dv_type)
    dv_type.sqref = "A2:A5000"

    # ── Boolean dropdowns ─────────────────────────────────────────────────────
    for field, col, dtype, req in list(_V_FIELDS) + list(_S_FIELDS):
        if dtype == "bool":
            col_letter = get_column_letter(col + 1)
            bv = DataValidation(
                type="list", formula1='"TRUE,FALSE"',
                allow_blank=True, showDropDown=False,
                showErrorMessage=True, errorTitle="Boolean field",
                error="Please enter TRUE or FALSE.",
            )
            ws.add_data_validation(bv)
            bv.sqref = f"{col_letter}2:{col_letter}5000"

    # ── Column letter shortcuts ───────────────────────────────────────────────
    mrp_col       = get_column_letter(20 + 1)   # U  MRP Price
    final_col     = get_column_letter(21 + 1)   # V  Final Price
    store_col     = get_column_letter(26 + 1)   # AA Store Name
    cgst_col      = get_column_letter(24 + 1)   # Y  CGST
    sgst_col      = get_column_letter(25 + 1)   # Z  SGST
    wh_col        = get_column_letter(27 + 1)   # AB Warehouse Name  ← NEW
    date_col      = get_column_letter(28 + 1)   # AC Date  ← NEW

    # ── Final Price <= MRP validation ─────────────────────────────────────────
    dv_final = DataValidation(
        type="decimal", operator="lessThanOrEqual",
        formula1=f"${mrp_col}2",
        allow_blank=True, showErrorMessage=True,
        errorTitle="Final Price too high",
        error=f"Final Price must be <= MRP Price ({mrp_col}).",
    )
    ws.add_data_validation(dv_final)
    dv_final.sqref = f"{final_col}2:{final_col}5000"

    # ── Date validation ────────────────────────────────────────────────────────
    # Use type="date" with DATE() formula so Excel accepts typed YYYY-MM-DD values.
    # type="date" with string formula1/formula2 is the correct openpyxl approach;
    # the earlier "2000-01-01" string form works when wrapped in DATE().
    dv_date = DataValidation(
        type="date",
        operator="between",
        formula1="DATE(2000,1,1)",
        formula2="DATE(2099,12,31)",
        allow_blank=True,
        showErrorMessage=True,
        showInputMessage=True,
        errorTitle="Invalid Date",
        error="Please enter a valid date in YYYY-MM-DD format (e.g. 2026-05-02).",
        promptTitle="Date*",
        prompt="Type a date: YYYY-MM-DD",
    )
    ws.add_data_validation(dv_date)
    dv_date.sqref = f"{date_col}2:{date_col}5000"

    # Format date column cells so Excel displays them as YYYY-MM-DD
    for row_i in range(2, 5001):
        c = ws.cell(row=row_i, column=28 + 1)
        c.number_format = "YYYY-MM-DD"

    # ── Conditional formatting ────────────────────────────────────────────────
    data_range = f"A2:{get_column_letter(_TOTAL_COLS)}5000"
    for keyword, fill_hex, font_hex in [
        ("PRODUCT", C_PROD_ROW, C_PROD_TYP),
        ("VARIANT", C_VAR_ROW,  C_VAR_TYP),
        ("SIZE",    C_SIZE_ROW, C_SIZE_TYP),
    ]:
        ws.conditional_formatting.add(
            data_range,
            FormulaRule(
                formula=[f'UPPER(TRIM($A2))="{keyword}"'],
                fill=PatternFill("solid", fgColor=fill_hex),
                font=Font(color=font_hex),
                stopIfTrue=False,
            ),
        )
    # Red highlight when Final Price exceeds MRP
    ws.conditional_formatting.add(
        f"{final_col}2:{final_col}5000",
        FormulaRule(
            formula=[f"AND({final_col}2<>\"\","
                     f"{mrp_col}2<>\"\",{final_col}2>{mrp_col}2)"],
            fill=PatternFill("solid", fgColor="FEE2E2"),
            font=Font(color="B91C1C", bold=True),
            stopIfTrue=True,
        ),
    )
    # Orange highlight when BOTH store and warehouse are filled (only one allowed)  ← NEW
    ws.conditional_formatting.add(
        f"{store_col}2:{store_col}5000",
        FormulaRule(
            formula=[f'AND({store_col}2<>"",{wh_col}2<>"")'],
            fill=PatternFill("solid", fgColor="FEF3C7"),
            font=Font(color="92400E", bold=True),
            stopIfTrue=True,
        ),
    )
    ws.conditional_formatting.add(
        f"{wh_col}2:{wh_col}5000",
        FormulaRule(
            formula=[f'AND({store_col}2<>"",{wh_col}2<>"")'],
            fill=PatternFill("solid", fgColor="FEF3C7"),
            font=Font(color="92400E", bold=True),
            stopIfTrue=True,
        ),
    )

    # ── Reference sheets (hidden) ─────────────────────────────────────────────
    def _make_ref_sheet(name, headers, rows_data):
        ws_r = wb.create_sheet(name)
        ws_r.sheet_state = "hidden"
        for ci, h in enumerate(headers, start=1):
            ws_r.cell(row=1, column=ci, value=h)
        for ri, row_vals in enumerate(rows_data, start=2):
            for ci, val in enumerate(row_vals, start=1):
                ws_r.cell(row=ri, column=ci, value=val)
        return ws_r, len(rows_data)

    cats    = _refs["categories"]
    _make_ref_sheet("CAT_REF",   ["CategoryId", "Name"],
                    [[r["id"], r["name"]] for r in cats])

    child_cats = _refs["childCategories"]
    _make_ref_sheet("CHILD_CAT_REF", ["CategoryId", "Name"],
                    [[r["id"], r["name"]] for r in child_cats])

    brands = _refs["brands"]
    _make_ref_sheet("BRAND_REF", ["brandId", "brandName"],
                    [[r["id"], r["name"]] for r in brands])

    fabrics = _refs["fabrics"]
    _make_ref_sheet("FABRIC_REF", ["FabricId", "Name"],
                    [[r["id"], r["name"]] for r in fabrics])

    hsn_codes = _refs["hsnCodes"]
    _make_ref_sheet("HSN_REF", ["TaxHSNCodeId", "HSNCode", "CGST", "SGST"],
                    [[r["id"], r["code"], r["cgst"], r["sgst"]] for r in hsn_codes])
    
    sizes = _refs["sizes"]
    _make_ref_sheet("SIZE_REF", ["SizeId", "SizeLabel"],
                [[r["id"], r["label"]] for r in sizes])

    cup_sizes = _refs["cupSizes"]
    _make_ref_sheet("CUP_SIZE_REF", ["CupSizeId", "CupSizeLabel"],
                    [[r["id"], r["label"]] for r in cup_sizes])

    # Store ref sheet
    ws_st = wb.create_sheet("STORES_REF")
    ws_st.sheet_state = "hidden"
    ws_st.cell(row=1, column=1, value="StoreId")
    ws_st.cell(row=1, column=2, value="StoreName")
    for i, store in enumerate(_stores, start=2):
        ws_st.cell(row=i, column=1, value=store["storeId"])
        ws_st.cell(row=i, column=2, value=store["storeName"])
    num_stores = len(_stores)

    # Warehouse ref sheet  ← NEW
    ws_wh = wb.create_sheet("WAREHOUSE_REF")
    ws_wh.sheet_state = "hidden"
    ws_wh.cell(row=1, column=1, value="WarehouseId")
    ws_wh.cell(row=1, column=2, value="WarehouseName")
    for i, wh in enumerate(_warehouses, start=2):
        ws_wh.cell(row=i, column=1, value=wh["warehouseId"])
        ws_wh.cell(row=i, column=2, value=wh["warehouseName"])
    num_warehouses = len(_warehouses)

    # ── Dropdown helper ───────────────────────────────────────────────────────
    def _add_list_dv(col_letter, sheet_name, num_rows, title="", err=""):
        if num_rows == 0:
            return
        ref = f"{sheet_name}!$B$2:$B${num_rows + 1}"
        dv  = DataValidation(
            type="list", formula1=f"={ref}",
            allow_blank=True, showDropDown=False,
            showErrorMessage=True,
            errorTitle=title or "Invalid value",
            error=err or "Please select from the dropdown.",
        )
        ws.add_data_validation(dv)
        dv.sqref = f"{col_letter}2:{col_letter}5000"

    cat_col        = get_column_letter(4 + 1)   # E  Category
    child_cat_col  = get_column_letter(5 + 1)   # F  Child Category
    brand_col      = get_column_letter(6 + 1)   # G  Brand
    fabric_col     = get_column_letter(10 + 1)  # K  Fabric
    hsn_col        = get_column_letter(15 + 1)  # P  HSN Code
    size_col     = get_column_letter(17 + 1)   # R  Size
    cup_size_col = get_column_letter(18 + 1)   # S  Cup Size

    _add_list_dv(cat_col,       "CAT_REF",       len(cats),        "Invalid Category",   "Select a category.")
    _add_list_dv(child_cat_col, "CHILD_CAT_REF", len(child_cats),  "Invalid Child Cat",  "Select a child category.")
    _add_list_dv(brand_col,     "BRAND_REF",     len(brands),      "Invalid Brand",      "Select a brand.")
    _add_list_dv(fabric_col,    "FABRIC_REF",    len(fabrics),     "Invalid Fabric",     "Select a fabric.")
    _add_list_dv(hsn_col,       "HSN_REF",       len(hsn_codes),   "Invalid HSN Code",   "Select an HSN code.")
    _add_list_dv(store_col,     "STORES_REF",    num_stores,       "Invalid Store",      "Select a store (or leave blank and fill Warehouse).")
    _add_list_dv(wh_col,        "WAREHOUSE_REF", num_warehouses,   "Invalid Warehouse",  "Select a warehouse (or leave blank and fill Store).")  # ← NEW
    _add_list_dv(size_col,     "SIZE_REF",     len(sizes),     "Invalid Size",     "Select a size.")
    _add_list_dv(cup_size_col, "CUP_SIZE_REF", len(cup_sizes), "Invalid Cup Size", "Select a cup size.")

    # ── Hidden VLOOKUP columns for IDs ────────────────────────────────────────
    # Col AD (30): CategoryId
    # Col AE (31): ChildCatId
    # Col AF (32): BrandId
    # Col AG (33): FabricId
    # Col AH (34): TaxHSNCodeId
    # Col AI (35): StoreId
    # Col AJ (36): WarehouseId  ← NEW
    hidden_lookups = [
        (30, cat_col,       "CAT_REF",       len(cats),        "CategoryId"),
        (31, child_cat_col, "CHILD_CAT_REF", len(child_cats),  "ChildCategoryId"),
        (32, brand_col,     "BRAND_REF",     len(brands),      "BrandId"),
        (33, fabric_col,    "FABRIC_REF",    len(fabrics),     "FabricId"),
        (34, hsn_col,       "HSN_REF",       len(hsn_codes),   "TaxHSNCodeId"),
        (35, store_col,     "STORES_REF",    num_stores,       "StoreId"),
        (36, wh_col,        "WAREHOUSE_REF", num_warehouses,   "WarehouseId"),  
        (37, size_col,     "SIZE_REF",     len(sizes),     "SizeId"),      # AK
        (38, cup_size_col, "CUP_SIZE_REF", len(cup_sizes), "CupSizeId"), 
    ]
    for abs_col, src_col, ref_sheet, n_rows, label in hidden_lookups:
        col_letter = get_column_letter(abs_col)
        ws.cell(row=1, column=abs_col, value=label).font = Font(color="FFFFFF", size=8)
        ws.column_dimensions[col_letter].hidden = True
        if n_rows > 0:
            for vrow in range(2, 501):
                formula = (
                    f'=IFERROR(VLOOKUP({src_col}{vrow},'
                    f'{ref_sheet}!$B$2:$A${n_rows+1},2,0),"")'
                )
                c = ws.cell(row=vrow, column=abs_col, value=formula)
                c.font = Font(color="FFFFFF", size=8)

    # ── CGST/SGST auto-fill formula ───────────────────────────────────────────
    hsn_id_col = get_column_letter(34)  # AH = TaxHSNCodeId hidden col
    n_hsn = len(hsn_codes)
    if n_hsn > 0:
        for vrow in range(2, 501):
            cgst_formula = (
                f'=IFERROR(VLOOKUP({hsn_id_col}{vrow},'
                f'HSN_REF!$A$2:$D${n_hsn+1},3,0),"")'
            )
            sgst_formula = (
                f'=IFERROR(VLOOKUP({hsn_id_col}{vrow},'
                f'HSN_REF!$A$2:$D${n_hsn+1},4,0),"")'
            )
            for col_i, formula in [(25, cgst_formula), (26, sgst_formula)]:
                c = ws.cell(row=vrow, column=col_i, value=formula)
                c.fill      = PatternFill("solid", fgColor=C_LOCK_BG)
                c.font      = Font(italic=True, color=C_LOCK_FONT, size=9)
                c.border    = thin
                c.alignment = Alignment(horizontal="center", vertical="center")
                c.protection = openpyxl.styles.Protection(locked=True)

    # ── Sample data ───────────────────────────────────────────────────────────
    today_str = datetime.now().strftime("%Y-%m-%d")

    sample_products = [
        {"productCode": "PRD-001", "name": "Product 1",
         "description": "First product description",
         "categoryName": "", "brandName": "", "childCategoryName": ""},
        {"productCode": "PRD-002", "name": "Product 2",
         "description": "Second product description",
         "categoryName": "", "brandName": "", "childCategoryName": ""},
    ]
    sample_variants = [
        {"variantCode": "VAR-RED",   "variantName": "Red Variant",   "color": "Red",
         "isBestSeller": "TRUE",  "isReturnAvailable": "TRUE",  "isCupSize": "FALSE",
         "variantDescription": "", "fabricName": "", "hsnCode": ""},
        {"variantCode": "VAR-BLUE",  "variantName": "Blue Variant",  "color": "Blue",
         "isBestSeller": "FALSE", "isReturnAvailable": "TRUE",  "isCupSize": "FALSE",
         "variantDescription": "", "fabricName": "", "hsnCode": ""},
        {"variantCode": "VAR-GREEN", "variantName": "Green Variant", "color": "Green",
         "isBestSeller": "FALSE", "isReturnAvailable": "FALSE", "isCupSize": "FALSE",
         "variantDescription": "", "fabricName": "", "hsnCode": ""},
    ]
    sample_sizes = [
        {"sizeId": 1, "cupSizeId": "", "stockId": "", "mrpPrice": 999,
         "finalPrice": 799, "availableQuantity": 50, "discountPercent": 20,
         "purchasePrice": 500, "cgst": "", "sgst": "",
         "storeName": "", "warehouseName": "", "entryDate": today_str},
        {"sizeId": 2, "cupSizeId": "", "stockId": "", "mrpPrice": 999,
         "finalPrice": 799, "availableQuantity": 30, "discountPercent": 20,
         "purchasePrice": 500, "cgst": "", "sgst": "",
         "storeName": "", "warehouseName": "", "entryDate": today_str},
    ]
    structure = [
        (sample_products[0], [sample_variants[0], sample_variants[1]], sample_sizes),
        (sample_products[1], [sample_variants[2]], sample_sizes),
    ]

    row_n = 2
    for prod, variants, sizes in structure:
        data_cell(row_n, _COL_ROW_TYPE, "PRODUCT", C_PROD_ROW, bold=True, color=C_PROD_TYP)
        for field, col, dtype, req in _P_FIELDS:
            data_cell(row_n, col, prod.get(field, ""), C_PROD_ROW)
        for field, col, dtype, req in list(_V_FIELDS) + list(_S_FIELDS):
            data_cell(row_n, col, "", C_BLANK)
        ws.row_dimensions[row_n].height = 20
        row_n += 1

        for var in variants:
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
                data_cell(row_n, _COL_ROW_TYPE, "SIZE", C_SIZE_ROW, color=C_SIZE_TYP)
                for field, col, dtype, req in list(_P_FIELDS) + list(_V_FIELDS):
                    data_cell(row_n, col, "", C_BLANK)
                for field, col, dtype, req in _S_FIELDS:
                    if field in ("cgst", "sgst"):
                        c = ws.cell(row=row_n, column=col + 1, value="")
                        c.fill      = PatternFill("solid", fgColor=C_LOCK_BG)
                        c.font      = Font(italic=True, color=C_LOCK_FONT, size=9)
                        c.border    = thin
                        c.alignment = Alignment(horizontal="center", vertical="center")
                    elif field == "entryDate":
                        # Write date value and apply date format  ← NEW
                        c = ws.cell(row=row_n, column=col + 1, value=sz.get(field, ""))
                        c.fill         = PatternFill("solid", fgColor=C_DATE_BG)
                        c.font         = Font(color=C_SIZE_TYP, size=10)
                        c.border       = thin
                        c.alignment    = Alignment(horizontal="center", vertical="center")
                        c.number_format = "YYYY-MM-DD"
                    else:
                        data_cell(row_n, col, sz.get(field, ""), C_SIZE_ROW, indent=2)
                ws.row_dimensions[row_n].height = 18
                row_n += 1

        blank_row(row_n)
        ws.row_dimensions[row_n].height = 6
        row_n += 1

    # ── 20 blank user-entry rows ──────────────────────────────────────────────
    for _ in range(20):
        for col_i in range(_TOTAL_COLS):
            col_field = next(
                (f for f, c_idx, _, _ in _S_FIELDS if c_idx == col_i), None
            )
            c = ws.cell(row=row_n, column=col_i + 1, value="")
            if col_field in ("cgst", "sgst"):
                c.fill       = PatternFill("solid", fgColor=C_LOCK_BG)
                c.font       = Font(italic=True, color=C_LOCK_FONT, size=9)
                c.protection = openpyxl.styles.Protection(locked=True)
            elif col_field == "entryDate":
                c.fill          = PatternFill("solid", fgColor=C_DATE_BG)
                c.number_format = "YYYY-MM-DD"          # ← NEW
            c.border = thin
        ws.row_dimensions[row_n].height = 18
        row_n += 1

    # ── Sheet protection: lock CGST/SGST, keep rest editable ─────────────────
    ws.protection.sheet    = True
    ws.protection.password = "twam_auto"
    ws.protection.enable()
    for row in ws.iter_rows(min_row=2, max_row=row_n - 1,
                             min_col=1, max_col=_TOTAL_COLS):
        for cell in row:
            if cell.column not in (25, 26):  # Y=25, Z=26 (1-based) are locked
                cell.protection = openpyxl.styles.Protection(locked=False)

    # ── Instruction note in row below data ────────────────────────────────────
    note_row = row_n
    note_cell = ws.cell(
        row=note_row,
        column=27,  # AB
        value="⚠ Fill EITHER Store Name OR Warehouse Name (not both). Date is required.",
    )
    note_cell.font      = Font(italic=True, color="92400E", size=9, bold=True)
    note_cell.fill      = PatternFill("solid", fgColor="FEF3C7")
    note_cell.alignment = Alignment(horizontal="left", wrap_text=True)
    ws.merge_cells(
        start_row=note_row, start_column=27,
        end_row=note_row,   end_column=29,
    )
    ws.row_dimensions[note_row].height = 28

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


# ==============================================================================
#  Hierarchical Excel parser
# ==============================================================================

def _parse_hierarchical_excel(file_bytes: bytes) -> list[dict]:
    _check_openpyxl()
    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Cannot read Excel file: {exc}")
    ws   = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise HTTPException(status_code=400, detail="The Excel file is empty.")

    current_product: dict    = {}
    current_variant: dict    = {}
    size_records: list[dict] = []

    for row_num, row in enumerate(rows[1:], start=2):
        if not row or len(row) == 0:
            continue
        raw_type = str(_cell(row, _COL_ROW_TYPE) or "").strip().upper().lstrip()
        if not raw_type or raw_type in ("", "ROW TYPE"):
            continue

        if "PRODUCT" in raw_type:
            current_product = {"_row": row_num}
            current_variant = {}
            for field, col, dtype, req in _P_FIELDS:
                try:
                    current_product[field] = _cast(_cell(row, col), dtype)
                except ValueError:
                    current_product[field] = None

        elif "VARIANT" in raw_type:
            current_variant = {"_row": row_num}
            for field, col, dtype, req in _V_FIELDS:
                try:
                    current_variant[field] = _cast(_cell(row, col), dtype)
                except ValueError:
                    current_variant[field] = None
            current_variant["_fabricName"] = current_variant.get("fabricName")
            current_variant["_hsnCode"]    = current_variant.get("hsnCode")

        elif "SIZE" in raw_type:
            if _is_sample_row({
                "productCode": current_product.get("productCode"),
                "variantCode": current_variant.get("variantCode"),
                "name":        current_product.get("name"),
            }):
                continue
            if not current_product or not current_variant:
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
                    errors.append(
                        f"Row {row_num}: Size field '{field}' has invalid value '{raw}'"
                    )
                    val = None
                # storeName and warehouseName are NOT individually required —
                # cross-validated below
                if req and field not in ("storeName", "warehouseName") and val is None:
                    errors.append(f"Row {row_num}: Size field '{field}' is required")
                rec[field] = val

            # ── Store / Warehouse cross-validation  ← NEW ─────────────────────
            store_val = rec.get("storeName") or ""
            wh_val    = rec.get("warehouseName") or ""
            if store_val and wh_val:
                errors.append(
                    f"Row {row_num}: Provide EITHER Store Name OR Warehouse Name, not both."
                )
            elif not store_val and not wh_val:
                errors.append(
                    f"Row {row_num}: Either Store Name or Warehouse Name is required."
                )

            # ── Date validation  ← NEW ────────────────────────────────────────
            if rec.get("entryDate") is None:
                errors.append(f"Row {row_num}: Date is required.")

            # Required product fields
            _P_REQUIRED_DISPLAY = {
                "productCode":  "Product Code (SKU)",
                "name":         "Product Name",
                "description":  "Description",
                "categoryName": "Category",
                "brandName":    "Brand",
            }
            for field, col, dtype, req in _P_FIELDS:
                if req:
                    val = current_product.get(field)
                    if not val or (isinstance(val, str) and not val.strip()):
                        label = _P_REQUIRED_DISPLAY.get(field, field)
                        errors.append(
                            f"Row {current_product.get('_row', '?')}: "
                            f"Product field '{label}' is required"
                        )

            # Required variant fields
            _V_REQUIRED_DISPLAY = {
                "variantCode": "Product Code",
                "variantName": "Variant Name",
                "fabricName":  "Fabric",
                "color":       "Color",
                "isCupSize":   "Is Cup Size",
            }
            for field, col, dtype, req in _V_FIELDS:
                if req:
                    val = current_variant.get(field)
                    missing = (
                        val is None
                        or (isinstance(val, str) and not val.strip())
                    )
                    if missing:
                        label = _V_REQUIRED_DISPLAY.get(field, field)
                        errors.append(
                            f"Row {current_variant.get('_row', '?')}: "
                            f"Variant field '{label}' is required"
                        )

            # Cup size cross-validation
            is_cup = current_variant.get("isCupSize")
            if isinstance(is_cup, str):
                is_cup = is_cup.strip().upper() == "TRUE"
            elif is_cup is None:
                is_cup = False
            cup_id = rec.get("cupSizeName") 
            if is_cup and not cup_id:
                errors.append(f"Row {row_num}: Is Cup Size = TRUE but Cup Size ID is missing")
            if not is_cup and cup_id:
                errors.append(f"Row {row_num}: Is Cup Size = FALSE but Cup Size ID should not be provided")

            mrp   = rec.get("mrpPrice")
            final = rec.get("finalPrice")
            if mrp is not None and final is not None and final > mrp:
                errors.append(f"Row {row_num}: Final Price ({final}) cannot exceed MRP ({mrp})")

            stock  = rec.get("availableQuantity") or 0
            is_out = stock == 0
            is_low = not is_out and stock < 10
            rec["isOutOfStock"] = is_out
            rec["isLowStock"]   = is_low

            rec["_errors"] = errors
            rec["_valid"]  = len(errors) == 0
            size_records.append(rec)

    return size_records


def _build_hierarchical_chains(
    size_records: list[dict],
    fabric_map: dict[str, int] | None = None,
    hsn_map_by_code: dict[str, dict] | None = None,
    cat_map: dict[str, int] | None = None,
    child_cat_map: dict[str, int] | None = None,
    brand_map: dict[str, int] | None = None,
    size_map=None,        # ← NEW: {sizeLabel -> sizeId}
    cup_size_map=None, 
) -> dict[str, dict]:
    chains: dict[str, dict] = {}

    for rec in size_records:
        if not rec["_valid"]:
            continue
        if _is_sample_row(rec):
            continue

        pcode = rec.get("productCode")
        vcode = rec.get("variantCode")
        if not pcode or not vcode:
            continue

        p_row = rec.get("_p_row")
        v_row = rec.get("_v_row")
        pkey  = f"p_row_{p_row}_{pcode}"
        vkey  = f"v_row_{v_row}_{vcode}"

        if pkey not in chains:
            p_rec = dict(rec)
            if cat_map:
                p_rec["categoryId"] = cat_map.get(p_rec.get("categoryName") or "", None)
            if child_cat_map:
                p_rec["childCategoryId"] = child_cat_map.get(
                    p_rec.get("childCategoryName") or "", None
                )
            if brand_map:
                p_rec["brandId"] = brand_map.get(p_rec.get("brandName") or "", None)
            chains[pkey] = {"rec": p_rec, "variants": {}}

        if vkey not in chains[pkey]["variants"]:
            v_rec = dict(rec)
            if fabric_map:
                fname = v_rec.get("fabricName") or ""
                v_rec["fabricId"] = fabric_map.get(fname)
            if hsn_map_by_code:
                hcode = v_rec.get("hsnCode") or ""
                hsn_entry = hsn_map_by_code.get(hcode)
                v_rec["variantTaxHSNCodeId"] = hsn_entry["id"] if hsn_entry else None
                v_rec["_cgst"] = hsn_entry["cgst"] if hsn_entry else None
                v_rec["_sgst"] = hsn_entry["sgst"] if hsn_entry else None
            chains[pkey]["variants"][vkey] = {"rec": v_rec, "slots": []}

        chains[pkey]["variants"][vkey]["slots"].append({
            "sizeId":    size_map.get(rec.get("sizeName") or "") if size_map else None,
            "cupSizeId": cup_size_map.get(rec.get("cupSizeName") or "") if cup_size_map else None,
            "stockId":           rec.get("stockId"),
            "mrpPrice":          rec["mrpPrice"],
            "finalPrice":        rec["finalPrice"],
            "availableQuantity": rec["availableQuantity"],
            "discountPercent":   rec.get("discountPercent") or 0,
            "purchasePrice":     rec.get("purchasePrice"),
            "cgst":              rec.get("cgst") or chains[pkey]["variants"][vkey]["rec"].get("_cgst"),
            "sgst":              rec.get("sgst") or chains[pkey]["variants"][vkey]["rec"].get("_sgst"),
            "storeName":         rec.get("storeName"),
            "storeId":           rec.get("storeId"),
            "warehouseName":     rec.get("warehouseName"),   # ← NEW
            "warehouseId":       rec.get("warehouseId"),     # ← NEW
            "entryDate":         rec.get("entryDate"),       # ← NEW
            "isLowStock":        rec.get("isLowStock", False),
            "isOutOfStock":      rec.get("isOutOfStock", False),
        })

    result = {}
    for pkey, chain in chains.items():
        variant_list = [v for v in chain["variants"].values() if v["slots"]]
        if variant_list:
            result[pkey] = {"rec": chain["rec"], "variants": variant_list}
    return result


def _collect_hsn_ids(chains: dict) -> list[int]:
    ids: set[int] = set()
    for chain in chains.values():
        for var in chain["variants"]:
            hsn_id = var["rec"].get("variantTaxHSNCodeId")
            if hsn_id:
                ids.add(int(hsn_id))
    return list(ids)


def _apply_hsn_rates(chains: dict, hsn_map: dict[int, dict]) -> list[dict]:
    """Inject CGST/SGST into slots from HSN map. Returns list of warnings."""
    warnings: list[dict] = []
    seen_missing: set[int] = set()

    for chain in chains.values():
        for var in chain["variants"]:
            hsn_id = var["rec"].get("variantTaxHSNCodeId")
            if not hsn_id:
                continue
            hsn_id = int(hsn_id)
            rates  = hsn_map.get(hsn_id)
            if rates:
                for slot in var["slots"]:
                    if slot.get("cgst") is None:
                        slot["cgst"] = rates["cgst"]
                    if slot.get("sgst") is None:
                        slot["sgst"] = rates["sgst"]
            elif hsn_id not in seen_missing:
                seen_missing.add(hsn_id)
                warnings.append({
                    "hsnCodeId":   hsn_id,
                    "variantCode": var["rec"].get("variantCode"),
                    "message": (
                        f"TaxHSNCodeId {hsn_id} not found in DB — "
                        "CGST/SGST will be NULL for this variant's sizes."
                    ),
                })
    return warnings


# ==============================================================================
#  SQL inserts
# ==============================================================================

def _pb_insert_product(db: Session, rec: dict) -> int:
    r = db.execute(text("""
        INSERT INTO twam."Products" (
            "ProductCode","Name","Description",
            "CategoryId","ChildCategoryId","BrandId","Tag",
            "State","CreatedDate","DeletedInd"
        ) VALUES (
            :productCode,:name,:description,
            :categoryId,:childCategoryId,:brandId,:tag,
            'Created',:now,false
        ) RETURNING "ProductId"
    """), {
        "productCode":     rec["productCode"],
        "name":            rec["name"],
        "description":     rec["description"],
        "categoryId":      rec.get("categoryId"),
        "childCategoryId": rec.get("childCategoryId"),
        "brandId":         rec.get("brandId"),
        "tag":             rec.get("tag"),
        "now":             NOW(),
    })
    return r.scalar_one()


def _pb_insert_variant(db: Session, rec: dict, product_id: int) -> int:
    r = db.execute(text("""
        INSERT INTO twam."ProductVariants" (
            "ProductId","ProductCode",
            "VariantName","VariantDescription",
            "FabricId","TaxHSNCodeId","Color",
            "IsBestSeller","IsReturnAvailable","IsCupSize",
            "CreatedDate"
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
    db: Session, slot: dict,
    product_id: int, variant_id: int,
    variant_code: str,
) -> int:
    r = db.execute(text("""
        INSERT INTO twam."ProductVariantDetail" (
            "ProductId","ProductVariantId","ProductCode",
            "Size","CupSize","StockId",
            "PurchasePrice",
            "AvailableQuantity",
            "ProcessedQuantity","ReturnedQuantity","AmendmentQuantity",
            "MRPPrice","FinalPrice","DiscountPercent",
            "CGST","SGST",
            "StoreId",
            "WarehouseId",
            "Date",
            "IsLowStock","IsOutOfStock",
            "State","CreatedDate","DeletedInd"
        ) VALUES (
            :productId,:productVariantId,:productCode,
            :size,:cupSize,:stockId,
            :purchasePrice,
            :availableQuantity,
            0,0,0,
            :mrpPrice,:finalPrice,:discountPercent,
            :cgst,:sgst,
            :storeId,
            :warehouseId,
            :entryDate,
            :isLowStock,:isOutOfStock,
            'Created',:now,false
        ) RETURNING "ProductVariantDetailId"
    """), {
        "productId":         product_id,
        "productVariantId":  variant_id,
        "productCode":       variant_code,
        "size":              int(slot["sizeId"]) if slot.get("sizeId") else None,
        "cupSize":           slot.get("cupSizeId"),
        "stockId":           slot.get("stockId"),
        "availableQuantity": slot["availableQuantity"],
        "mrpPrice":          slot["mrpPrice"],
        "finalPrice":        slot["finalPrice"],
        "discountPercent":   slot.get("discountPercent") or 0,
        "purchasePrice":     slot.get("purchasePrice"),
        "cgst":              slot.get("cgst"),
        "sgst":              slot.get("sgst"),
        "storeId":           slot.get("storeId"),
        "warehouseId":       slot.get("warehouseId"),    # ← NEW
        "entryDate":         slot.get("entryDate"),      # ← NEW
        "isLowStock":        slot.get("isLowStock", False),
        "isOutOfStock":      slot.get("isOutOfStock", False),
        "now":               NOW(),
    })
    return r.scalar_one()


# ==============================================================================
#  ROUTES — Generic entities
# ==============================================================================

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
    border   = _thin_border()
    center   = _center_align()
    hdr_font = Font(bold=True, color="FFFFFF", size=11)
    hdr_fill = PatternFill("solid", fgColor=BRAND_RED)
    req_fill = PatternFill("solid", fgColor=LIGHT_PINK)
    for col_idx, (field, label, dtype, req) in enumerate(col_defs, start=1):
        c = ws.cell(row=1, column=col_idx, value=label)
        c.font = hdr_font; c.fill = hdr_fill; c.alignment = center; c.border = border
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
        ws.cell(row=i, column=1, value=f"* {note}").font = note_font
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
async def preview_entity(entity: str, file: UploadFile = File(...)):
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


# ==============================================================================
#  ROUTE — HSN Tax Info
# ==============================================================================

@router.get("/hsn-tax-info", summary="Get CGST/SGST rates for one or more TaxHSNCodeIds")
async def get_hsn_tax_info(
    codes: str = Query(..., description="Comma-separated TaxHSNCodeIds, e.g. 1,2,3"),
    db: Session = Depends(get_db),
):
    try:
        ids = [int(x.strip()) for x in codes.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="'codes' must be comma-separated integers.")
    hsn_map = await _fetch_hsn_rates(db, ids) if ids else {}
    found   = {k: v for k, v in hsn_map.items() if k in ids}
    missing = [i for i in ids if i not in found]
    return {
        "found":   found,
        "missing": missing,
        "message": (
            f"{len(missing)} HSN code(s) not found in DB."
        ) if missing else "All HSN codes resolved.",
    }


# ==============================================================================
#  ROUTES — Product Bulk
# ==============================================================================

@router.get("/stores", summary="List all active stores")
async def list_stores(db: Session = Depends(get_db)):
    stores = _fetch_all_stores(db)
    return {"stores": stores, "total": len(stores)}


@router.get("/warehouses", summary="List all active warehouses")   # ← NEW ROUTE
async def list_warehouses(db: Session = Depends(get_db)):
    warehouses = _fetch_all_warehouses(db)
    return {"warehouses": warehouses, "total": len(warehouses)}


@router.get(
    "/product-bulk/template",
    summary="Download hierarchical Excel template (Product / Variant / Size rows)",
)
def download_product_bulk_template(db: Session = Depends(get_db)):
    stores     = _fetch_all_stores(db)
    warehouses = _fetch_all_warehouses(db)   # ← NEW
    refs       = _fetch_ref_data(db)
    xlsx_bytes = _build_hierarchical_template(stores, refs, warehouses)
    return StreamingResponse(
        io.BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="twam_product_bulk_template.xlsx"'},
    )


@router.post(
    "/product-bulk/preview",
    summary="Preview hierarchical product bulk file — validation + HSN warnings, no DB write",
)
async def preview_product_bulk(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    content      = await file.read()
    size_records = _parse_hierarchical_excel(content)

    refs            = _fetch_ref_data(db)
    fabric_map      = {r["name"]: r["id"] for r in refs["fabrics"]}
    hsn_map_by_code = {r["code"]: r for r in refs["hsnCodes"]}
    cat_map         = {r["name"]: r["id"] for r in refs["categories"]}
    child_cat_map   = {r["name"]: r["id"] for r in refs["childCategories"]}
    brand_map       = {r["name"]: r["id"] for r in refs["brands"]}
    size_map     = {r["label"]: r["id"] for r in refs["sizes"]}
    cup_size_map = {r["label"]: r["id"] for r in refs["cupSizes"]}
    chains = _build_hierarchical_chains(
        size_records, fabric_map, hsn_map_by_code,
        cat_map, child_cat_map, brand_map,size_map=size_map,
        cup_size_map=cup_size_map,
    )

    hsn_ids      = _collect_hsn_ids(chains)
    hsn_map      = await _fetch_hsn_rates(db, hsn_ids)
    hsn_warnings = _apply_hsn_rates(chains, hsn_map)

    # Resolve storeName -> storeId
    store_map = _fetch_store_map(db)
    store_warnings: list[dict] = []
    seen_bad_stores: set[str] = set()

    # Resolve warehouseName -> warehouseId  ← NEW
    warehouse_map = _fetch_warehouse_map(db)
    warehouse_warnings: list[dict] = []
    seen_bad_warehouses: set[str] = set()

    for chain in chains.values():
        for var in chain["variants"]:
            for slot in var["slots"]:
                # Store resolution
                sname = slot.get("storeName") or ""
                if sname:
                    sid = store_map.get(sname)
                    slot["storeId"] = sid
                    if not sid and sname not in seen_bad_stores:
                        seen_bad_stores.add(sname)
                        store_warnings.append({
                            "storeName":   sname,
                            "variantCode": var["rec"].get("variantCode"),
                            "message":     f"Store '{sname}' not found in DB — StoreId will be NULL.",
                        })

                # Warehouse resolution  ← NEW
                wname = slot.get("warehouseName") or ""
                if wname:
                    wid = warehouse_map.get(wname)
                    slot["warehouseId"] = wid
                    if not wid and wname not in seen_bad_warehouses:
                        seen_bad_warehouses.add(wname)
                        warehouse_warnings.append({
                            "warehouseName": wname,
                            "variantCode":   var["rec"].get("variantCode"),
                            "message":       f"Warehouse '{wname}' not found in DB — WarehouseId will be NULL.",
                        })

    preview_rows = []
    for rec in size_records:
        preview_rows.append({
            "row":            rec["_row"],
            "status":         "OK" if rec["_valid"] else "Error",
            "issues":         rec["_errors"],
            "productCode":    rec.get("productCode"),
            "productName":    rec.get("name"),
            "variantCode":    rec.get("variantCode"),
            "variantName":    rec.get("variantName"),
            "fabricName":     rec.get("fabricName"),
            "hsnCode":        rec.get("hsnCode"),
            "stockId":        rec.get("stockId"),
            "color":          rec.get("color"),
            "sizeId":         rec.get("sizeId"),
            "cupSizeId":      rec.get("cupSizeId"),
            "mrpPrice":       rec.get("mrpPrice"),
            "finalPrice":     rec.get("finalPrice"),
            "purchasePrice":  rec.get("purchasePrice"),
            "stockQty":       rec.get("availableQuantity"),
            "cgst":           rec.get("cgst"),
            "sgst":           rec.get("sgst"),
            "storeName":      rec.get("storeName"),
            "warehouseName":  rec.get("warehouseName"),   # ← NEW
            "entryDate":      str(rec["entryDate"]) if rec.get("entryDate") else None,  # ← NEW
            "isLowStock":     rec.get("isLowStock", False),
            "isOutOfStock":   rec.get("isOutOfStock", False),
        })

    return {
        "totalSizeRows":       len(size_records),
        "validRows":           sum(1 for r in size_records if r["_valid"]),
        "errorRows":           sum(1 for r in size_records if not r["_valid"]),
        "willImportProducts":  len(chains),
        "willImportVariants":  sum(len(v["variants"]) for v in chains.values()),
        "willImportDetails":   sum(
            len(var["slots"])
            for v in chains.values()
            for var in v["variants"]
        ),
        "canImport":           len(chains) > 0,
        "hsnWarnings":         hsn_warnings,
        "storeWarnings":       store_warnings,
        "warehouseWarnings":   warehouse_warnings,   # ← NEW
        "rows":                preview_rows[:500],
    }


@router.post(
    "/product-bulk/import",
    summary="Execute hierarchical product bulk import — full rollback on DB error",
)
async def import_product_bulk(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    if current_user.role_id not in {Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER, Roles.INVENTORY_MANAGER}:
        raise HTTPException(status_code=403, detail="Insufficient permissions.")

    content      = await file.read()
    size_records = _parse_hierarchical_excel(content)

    refs            = _fetch_ref_data(db)
    fabric_map      = {r["name"]: r["id"] for r in refs["fabrics"]}
    hsn_map_by_code = {r["code"]: r for r in refs["hsnCodes"]}
    cat_map         = {r["name"]: r["id"] for r in refs["categories"]}
    child_cat_map   = {r["name"]: r["id"] for r in refs["childCategories"]}
    brand_map       = {r["name"]: r["id"] for r in refs["brands"]}
    size_map     = {r["label"]: r["id"] for r in refs["sizes"]}
    cup_size_map = {r["label"]: r["id"] for r in refs["cupSizes"]}
    chains = _build_hierarchical_chains(
        size_records, fabric_map, hsn_map_by_code,
        cat_map, child_cat_map, brand_map, size_map=size_map, cup_size_map=cup_size_map
    )

    hsn_ids      = _collect_hsn_ids(chains)
    hsn_map      = await _fetch_hsn_rates(db, hsn_ids)
    hsn_warnings = _apply_hsn_rates(chains, hsn_map)

    # Resolve storeName -> storeId
    store_map = _fetch_store_map(db)
    store_warnings: list[dict] = []
    seen_bad_stores: set[str] = set()

    # Resolve warehouseName -> warehouseId  ← NEW
    warehouse_map = _fetch_warehouse_map(db)
    warehouse_warnings: list[dict] = []
    seen_bad_warehouses: set[str] = set()

    for chain in chains.values():
        for var in chain["variants"]:
            for slot in var["slots"]:
                sname = slot.get("storeName") or ""
                if sname:
                    sid = store_map.get(sname)
                    slot["storeId"] = sid
                    if not sid and sname not in seen_bad_stores:
                        seen_bad_stores.add(sname)
                        store_warnings.append({
                            "storeName":   sname,
                            "variantCode": var["rec"].get("variantCode"),
                            "message":     f"Store '{sname}' not found in DB — StoreId will be NULL.",
                        })

                wname = slot.get("warehouseName") or ""
                if wname:
                    wid = warehouse_map.get(wname)
                    slot["warehouseId"] = wid
                    if not wid and wname not in seen_bad_warehouses:
                        seen_bad_warehouses.add(wname)
                        warehouse_warnings.append({
                            "warehouseName": wname,
                            "variantCode":   var["rec"].get("variantCode"),
                            "message":       f"Warehouse '{wname}' not found in DB — WarehouseId will be NULL.",
                        })

    skipped_rows = sum(1 for r in size_records if not r["_valid"])

    if not chains:
        return {
            "message":          "No valid rows to import after filtering out errors.",
            "insertedProducts": 0,
            "insertedVariants": 0,
            "insertedDetails":  0,
            "skippedRows":      skipped_rows,
            "hsnWarnings":      hsn_warnings,
            "warehouseWarnings": warehouse_warnings,
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

        db.commit()

    except Exception as exc:
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
        "insertedProducts":  inserted_products,
        "insertedVariants":  inserted_variants,
        "insertedDetails":   inserted_details,
        "skippedRows":       skipped_rows,
        "hsnWarnings":       hsn_warnings,
        "storeWarnings":     store_warnings,
        "warehouseWarnings": warehouse_warnings,   # ← NEW
    }