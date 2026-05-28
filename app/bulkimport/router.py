"""
app/bulkimport/router.py
"""

import io
import logging
from datetime import datetime, timezone, timedelta, date
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.formatting.rule import FormulaRule

try:
    import openpyxl
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter, quote_sheetname
    from openpyxl.workbook.defined_name import DefinedName
    OPENPYXL_AVAILABLE = True
except ImportError:
    OPENPYXL_AVAILABLE = False

from database import get_db
from app.auth.dependencies import get_current_user, Roles, CurrentUser

logger = logging.getLogger(__name__)

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
            if isinstance(value, (int, float)):
                d = datetime(1899, 12, 30) + timedelta(days=int(value))
                return d.date()
            if isinstance(value, datetime):
                return value.date()
            if isinstance(value, date):
                return value
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
#  HSN / Store / Warehouse / Ref data helpers
# ==============================================================================

async def _fetch_hsn_rates(db: Session, hsn_ids: list[int]) -> dict[int, dict]:
    if not hsn_ids:
        return {}
    result = db.execute(
        text("""
            SELECT "TaxHSNCodeId", "Slab1CGST", "Slab1SGST"
            FROM   mdm."TaxHSNCode"
            WHERE  "TaxHSNCodeId" = ANY(:ids)
              AND  "IsActive" = true
              AND  "DeletedInd" = false
        """),
        {"ids": hsn_ids},
    )
    return {
        row.TaxHSNCodeId: {"cgst": float(row.Slab1CGST or 0), "sgst": float(row.Slab1SGST or 0)}
        for row in result
    }


def _fetch_all_stores(db: Session) -> list[dict]:
    result = db.execute(text("""
        SELECT "StoreId", "StoreName"
        FROM   twam."Stores"
        WHERE  "IsActive" = true AND "DeletedInd" = false
        ORDER  BY "StoreName"
    """))
    return [{"storeId": row.StoreId, "storeName": row.StoreName} for row in result]


def _fetch_store_map(db: Session) -> dict[str, int]:
    return {s["storeName"]: s["storeId"] for s in _fetch_all_stores(db)}


def _fetch_all_warehouses(db: Session) -> list[dict]:
    result = db.execute(text("""
        SELECT "WarehouseId", "WarehouseName"
        FROM   twam."Warehouses"
        WHERE  "IsActive" = true
        ORDER  BY "WarehouseName"
    """))
    return [{"warehouseId": row.WarehouseId, "warehouseName": row.WarehouseName}
            for row in result]


def _fetch_warehouse_map(db: Session) -> dict[str, int]:
    return {w["warehouseName"]: w["warehouseId"] for w in _fetch_all_warehouses(db)}


def _fetch_ref_data(db: Session) -> dict:
    cats = db.execute(text("""
        SELECT "CategoryId", "Name", "ParentCategoryId"
        FROM   twam."Category"
        WHERE  "IsActive" = true AND "DeletedInd" = false ORDER BY "Name"
    """))
    cat_rows = [{"id": r.CategoryId, "name": r.Name, "parent": r.ParentCategoryId} for r in cats]

    brands = db.execute(text("""
        SELECT "brandId", "brandName" FROM mdm."Brand"
        WHERE  "isActive" = true AND "deletedInd" = false ORDER BY "brandName"
    """))
    brand_rows = [{"id": r.brandId, "name": r.brandName} for r in brands]

    fabrics = db.execute(text("""
        SELECT "FabricId", "Name" FROM mdm."Fabric"
        WHERE  "IsActive" = true AND "DeletedInd" = false ORDER BY "Name"
    """))
    fabric_rows = [{"id": r.FabricId, "name": r.Name} for r in fabrics]

    hsn = db.execute(text("""
        SELECT "TaxHSNCodeId", "HSNCode", "HSNDescription", "Slab1CGST", "Slab1SGST"
        FROM   mdm."TaxHSNCode"
        WHERE  "IsActive" = true AND "DeletedInd" = false ORDER BY "HSNCode"
    """))
    hsn_rows = [{"id": r.TaxHSNCodeId, "code": r.HSNCode, "desc": r.HSNDescription,
                 "cgst": float(r.Slab1CGST or 0), "sgst": float(r.Slab1SGST or 0)} for r in hsn]

    sizes = db.execute(text("""
        SELECT "SizeId", "SizeLabel", "SizeCode" FROM mdm."Size"
        WHERE  "IsActive" = true AND "DeletedInd" = false ORDER BY "OrderNo", "SizeLabel"
    """))
    size_rows = [{"id": r.SizeId, "label": r.SizeLabel, "code": r.SizeCode} for r in sizes]

    cup_sizes = db.execute(text("""
        SELECT "CupSizeId", "SizeLabel" FROM mdm."CupSize"
        WHERE  "IsActive" = true AND "DeletedInd" = false ORDER BY "SizeLabel"
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


def _fetch_existing_stock_ids(db: Session, stock_ids: list[int]) -> set[int]:
    """Return set of StockIds (as int) that already exist in ProductVariantDetail."""
    if not stock_ids:
        return set()
    # Deduplicate and ensure all are int
    int_ids = list({int(s) for s in stock_ids if s is not None})
    if not int_ids:
        return set()
    result = db.execute(
        text("""
            SELECT DISTINCT "StockId"
            FROM   twam."ProductVariantDetail"
            WHERE  "StockId" = ANY(:ids)
            AND    "DeletedInd" = false
        """),
        {"ids": int_ids},
    )
    return {int(row.StockId) for row in result}


# ==============================================================================
#  Column definitions
# ==============================================================================

_COL_ROW_TYPE = 0  # A

_P_FIELDS: list[tuple[str, int, str, bool]] = [
    ("productCode",       1, "str", True),
    ("name",              2, "str", True),
    ("description",       3, "str", True),
    ("categoryName",      4, "str", True),
    ("childCategoryName", 5, "str", False),
    ("brandName",         6, "str", True),
]

_V_FIELDS: list[tuple[str, int, str, bool]] = [
    ("variantCode",        7,  "str",  True),
    ("variantName",        8,  "str",  True),
    ("variantDescription", 9,  "str",  False),
    ("fabricName",         10, "str",  False),  # optional — fabric table may be empty
    ("color",              11, "str",  True),
    ("isBestSeller",       12, "bool", False),
    ("isReturnAvailable",  13, "bool", False),
    ("isCupSize",          14, "bool", True),
    ("hsnCode",            15, "str",  False),
]

_S_FIELDS: list[tuple[str, int, str, bool]] = [
    ("stockId",           16, "int",   True),
    ("sizeName",          17, "str",   True),
    ("cupSizeName",       18, "str",   False),
    ("purchasePrice",     19, "int",   True),
    ("mrpPrice",          20, "float", True),
    ("finalPrice",        21, "float", True),
    ("discountPercent",   22, "int",   False),
    ("availableQuantity", 23, "int",   True),
    ("cgst",              24, "float", False),
    ("sgst",              25, "float", False),
    ("storeName",         26, "str",   False),
    ("warehouseName",     27, "str",   False),
    ("entryDate",         28, "date",  True),
]

# Hidden VLOOKUP columns written by the template (read-only, used directly)
# These are already resolved IDs — trust them over re-doing name lookups
_COL_CATEGORY_ID   = 29   # AD — CategoryId    (from PRODUCT row)
_COL_CHILD_CAT_ID  = 30   # AE — ChildCategoryId
_COL_BRAND_ID      = 31   # AF — BrandId
_COL_FABRIC_ID     = 32   # AG — FabricId       (from VARIANT row)
_COL_HSN_ID        = 33   # AH — TaxHSNCodeId
_COL_STORE_ID      = 34   # AI — StoreId        (from SIZE row)
_COL_WAREHOUSE_ID  = 35   # AJ — WarehouseId
_COL_SIZE_ID       = 36   # AK — SizeId
_COL_CUP_SIZE_ID   = 37   # AL — CupSizeId

_TOTAL_COLS = 29  # A–AC (visible columns only)


def _cell(row: tuple, col: int) -> Any:
    return row[col] if col < len(row) else None


def _is_sample_row(rec: dict) -> bool:
    """Detect template sample rows that should be skipped."""
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
#  Template builder
# ==============================================================================

def _build_hierarchical_template(_stores: list[dict], _refs: dict, _warehouses: list[dict]) -> bytes:
    _check_openpyxl()

    C_PROD_HDR  = "6B0F2A"
    C_VAR_HDR   = "1A3A5C"
    C_SIZE_HDR  = "14532D"
    C_TAX_HDR   = "7C6000"
    C_STORE_HDR = "1E3A5F"
    C_WH_HDR    = "4A1D96"
    C_DATE_HDR  = "065F46"
    C_TYPE_HDR  = "2D2D2D"
    C_PROD_ROW  = "FFF0F3"
    C_VAR_ROW   = "EFF6FF"
    C_SIZE_ROW  = "F0FDF4"
    C_PROD_TYP  = "6B0F2A"
    C_VAR_TYP   = "1D4ED8"
    C_SIZE_TYP  = "047857"
    C_BLANK     = "F5F5F5"
    C_SEP       = "EFEFEF"
    C_LOCK_BG   = "D1D5DB"
    C_LOCK_FONT = "6B7280"
    C_DATE_BG   = "ECFDF5"

    wb   = Workbook()
    ws   = wb.active
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

    # ── Headers ───────────────────────────────────────────────────────────────
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
        "stockId":           "Stock ID*",
        "sizeName":          "Size*",
        "cupSizeName":       "Cup Size",
        "purchasePrice":     "Purchase Price*",
        "mrpPrice":          "MRP Price*",
        "finalPrice":        "Final Price*",
        "discountPercent":   "Discount %",
        "availableQuantity": "Stock Qty*",
        "cgst":              "CGST % (auto)",
        "sgst":              "SGST % (auto)",
        "storeName":         "Store Name (or Warehouse)",
        "warehouseName":     "Warehouse Name (or Store)",
        "entryDate":         "Date*",
    }
    for field, col, dtype, req in _S_FIELDS:
        bg = (C_TAX_HDR   if field in ("cgst", "sgst") else
              C_STORE_HDR if field == "storeName"       else
              C_WH_HDR    if field == "warehouseName"   else
              C_DATE_HDR  if field == "entryDate"       else
              C_SIZE_HDR)
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

    # ── Column letters ────────────────────────────────────────────────────────
    mrp_col   = get_column_letter(20 + 1)  # U
    final_col = get_column_letter(21 + 1)  # V
    store_col = get_column_letter(26 + 1)  # AA
    wh_col    = get_column_letter(27 + 1)  # AB
    date_col  = get_column_letter(28 + 1)  # AC

    # ── Final Price <= MRP ────────────────────────────────────────────────────
    dv_final = DataValidation(
        type="decimal", operator="lessThanOrEqual",
        formula1=f"${mrp_col}2", allow_blank=True,
        showErrorMessage=True, errorTitle="Final Price too high",
        error=f"Final Price must be <= MRP Price.",
    )
    ws.add_data_validation(dv_final)
    dv_final.sqref = f"{final_col}2:{final_col}5000"

    # ── Date validation ───────────────────────────────────────────────────────
    dv_date = DataValidation(
        type="date", operator="between",
        formula1="DATE(2000,1,1)", formula2="DATE(2099,12,31)",
        allow_blank=True, showErrorMessage=True, showInputMessage=True,
        errorTitle="Invalid Date",
        error="Please enter a valid date in YYYY-MM-DD format.",
        promptTitle="Date*", prompt="Type a date: YYYY-MM-DD",
    )
    ws.add_data_validation(dv_date)
    dv_date.sqref = f"{date_col}2:{date_col}5000"

    for row_i in range(2, 5001):
        ws.cell(row=row_i, column=28 + 1).number_format = "YYYY-MM-DD"

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
    ws.conditional_formatting.add(
        f"{final_col}2:{final_col}5000",
        FormulaRule(
            formula=[f"AND({final_col}2<>\"\",{mrp_col}2<>\"\",{final_col}2>{mrp_col}2)"],
            fill=PatternFill("solid", fgColor="FEE2E2"),
            font=Font(color="B91C1C", bold=True), stopIfTrue=True,
        ),
    )
    ws.conditional_formatting.add(
        f"{store_col}2:{store_col}5000",
        FormulaRule(
            formula=[f'AND({store_col}2<>"",{wh_col}2<>"")'],
            fill=PatternFill("solid", fgColor="FEF3C7"),
            font=Font(color="92400E", bold=True), stopIfTrue=True,
        ),
    )
    ws.conditional_formatting.add(
        f"{wh_col}2:{wh_col}5000",
        FormulaRule(
            formula=[f'AND({store_col}2<>"",{wh_col}2<>"")'],
            fill=PatternFill("solid", fgColor="FEF3C7"),
            font=Font(color="92400E", bold=True), stopIfTrue=True,
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
        return len(rows_data)

    cats       = _refs["categories"]
    child_cats = _refs["childCategories"]
    brands     = _refs["brands"]
    fabrics    = _refs["fabrics"]
    hsn_codes  = _refs["hsnCodes"]
    sizes      = _refs["sizes"]
    cup_sizes  = _refs["cupSizes"]

    n_cats       = _make_ref_sheet("CAT_REF",       ["CategoryId","Name"],          [[r["id"],r["name"]] for r in cats])
    n_child_cats = _make_ref_sheet("CHILD_CAT_REF", ["CategoryId","Name"],          [[r["id"],r["name"]] for r in child_cats])
    n_brands     = _make_ref_sheet("BRAND_REF",     ["brandId","brandName"],        [[r["id"],r["name"]] for r in brands])
    n_fabrics    = _make_ref_sheet("FABRIC_REF",    ["FabricId","Name"],            [[r["id"],r["name"]] for r in fabrics])
    n_hsn        = _make_ref_sheet("HSN_REF",       ["TaxHSNCodeId","HSNCode","CGST","SGST"], [[r["id"],r["code"],r["cgst"],r["sgst"]] for r in hsn_codes])
    n_sizes      = _make_ref_sheet("SIZE_REF",      ["SizeId","SizeLabel"],         [[r["id"],r["label"]] for r in sizes])
    n_cup_sizes  = _make_ref_sheet("CUP_SIZE_REF",  ["CupSizeId","CupSizeLabel"],   [[r["id"],r["label"]] for r in cup_sizes])

    ws_st = wb.create_sheet("STORES_REF")
    ws_st.sheet_state = "hidden"
    ws_st.cell(row=1, column=1, value="StoreId")
    ws_st.cell(row=1, column=2, value="StoreName")
    for i, store in enumerate(_stores, start=2):
        ws_st.cell(row=i, column=1, value=store["storeId"])
        ws_st.cell(row=i, column=2, value=store["storeName"])
    n_stores = len(_stores)

    ws_wh = wb.create_sheet("WAREHOUSE_REF")
    ws_wh.sheet_state = "hidden"
    ws_wh.cell(row=1, column=1, value="WarehouseId")
    ws_wh.cell(row=1, column=2, value="WarehouseName")
    for i, wh in enumerate(_warehouses, start=2):
        ws_wh.cell(row=i, column=1, value=wh["warehouseId"])
        ws_wh.cell(row=i, column=2, value=wh["warehouseName"])
    n_warehouses = len(_warehouses)

    # ── Named ranges → dropdowns (works with hidden sheets) ──────────────────
    # Excel silently ignores cross-sheet list validation when source sheet is hidden.
    # Named ranges bypass this — they resolve at workbook level before sheet visibility.
    def _named_range_dv(col_letter: str, range_name: str, n_rows: int, title="", err=""):
        if n_rows == 0:
            return
        dv = DataValidation(
            type="list",
            formula1=range_name,   # just the named range — no = prefix
            allow_blank=True,
            showDropDown=False,
            showErrorMessage=True,
            errorTitle=title or "Invalid value",
            error=err or "Please select from the dropdown.",
        )
        ws.add_data_validation(dv)
        dv.sqref = f"{col_letter}2:{col_letter}5000"

    def _reg_nr(range_name: str, sheet_name: str, n_rows: int):
        if n_rows == 0:
            return
        formula = f"{quote_sheetname(sheet_name)}!$B$2:$B${n_rows + 1}"
        wb.defined_names.add(DefinedName(name=range_name, attr_text=formula))

    # Register all named ranges
    _reg_nr("CATEGORY_LIST",  "CAT_REF",       n_cats)
    _reg_nr("CHILD_CAT_LIST", "CHILD_CAT_REF", n_child_cats)
    _reg_nr("BRAND_LIST",     "BRAND_REF",     n_brands)
    _reg_nr("FABRIC_LIST",    "FABRIC_REF",    n_fabrics)
    _reg_nr("HSN_LIST",       "HSN_REF",       n_hsn)
    _reg_nr("STORE_LIST",     "STORES_REF",    n_stores)
    _reg_nr("WAREHOUSE_LIST", "WAREHOUSE_REF", n_warehouses)
    _reg_nr("SIZE_LIST",      "SIZE_REF",      n_sizes)
    _reg_nr("CUP_SIZE_LIST",  "CUP_SIZE_REF",  n_cup_sizes)

    cat_col       = get_column_letter(4 + 1)   # E
    child_cat_col = get_column_letter(5 + 1)   # F
    brand_col     = get_column_letter(6 + 1)   # G
    fabric_col    = get_column_letter(10 + 1)  # K
    hsn_col       = get_column_letter(15 + 1)  # P
    size_col      = get_column_letter(17 + 1)  # R
    cup_size_col  = get_column_letter(18 + 1)  # S

    _named_range_dv(cat_col,       "CATEGORY_LIST",  n_cats,       "Invalid Category",  "Select a category.")
    _named_range_dv(child_cat_col, "CHILD_CAT_LIST", n_child_cats, "Invalid Child Cat", "Select a child category.")
    _named_range_dv(brand_col,     "BRAND_LIST",     n_brands,     "Invalid Brand",     "Select a brand.")
    _named_range_dv(fabric_col,    "FABRIC_LIST",    n_fabrics,    "Invalid Fabric",    "Select a fabric.")
    _named_range_dv(hsn_col,       "HSN_LIST",       n_hsn,        "Invalid HSN Code",  "Select an HSN code.")
    _named_range_dv(store_col,     "STORE_LIST",     n_stores,     "Invalid Store",     "Select a store (or leave blank and fill Warehouse).")
    _named_range_dv(wh_col,        "WAREHOUSE_LIST", n_warehouses, "Invalid Warehouse", "Select a warehouse (or leave blank and fill Store).")
    _named_range_dv(size_col,      "SIZE_LIST",      n_sizes,      "Invalid Size",      "Select a size.")
    _named_range_dv(cup_size_col,  "CUP_SIZE_LIST",  n_cup_sizes,  "Invalid Cup Size",  "Select a cup size.")

    # ── Hidden VLOOKUP columns for IDs (INDEX/MATCH — works left-to-right) ───
    # Col AD(30)=CategoryId, AE(31)=ChildCatId, AF(32)=BrandId, AG(33)=FabricId
    # AH(34)=TaxHSNCodeId, AI(35)=StoreId, AJ(36)=WarehouseId
    # AK(37)=SizeId, AL(38)=CupSizeId
    hidden_lookups = [
        (30, cat_col,       "CAT_REF",       n_cats,       "CategoryId"),
        (31, child_cat_col, "CHILD_CAT_REF", n_child_cats, "ChildCategoryId"),
        (32, brand_col,     "BRAND_REF",     n_brands,     "BrandId"),
        (33, fabric_col,    "FABRIC_REF",    n_fabrics,    "FabricId"),
        (34, hsn_col,       "HSN_REF",       n_hsn,        "TaxHSNCodeId"),
        (35, store_col,     "STORES_REF",    n_stores,     "StoreId"),
        (36, wh_col,        "WAREHOUSE_REF", n_warehouses, "WarehouseId"),
        (37, size_col,      "SIZE_REF",      n_sizes,      "SizeId"),
        (38, cup_size_col,  "CUP_SIZE_REF",  n_cup_sizes,  "CupSizeId"),
    ]
    for abs_col, src_col, ref_sheet, n_rows, label in hidden_lookups:
        col_letter = get_column_letter(abs_col)
        ws.cell(row=1, column=abs_col, value=label).font = Font(color="FFFFFF", size=8)
        ws.column_dimensions[col_letter].hidden = True
        if n_rows > 0:
            for vrow in range(2, 501):
                # INDEX/MATCH: ID is col A, Name is col B → look up name, return id
                formula = (
                    f'=IFERROR(INDEX({ref_sheet}!$A$2:$A${n_rows+1},'
                    f'MATCH({src_col}{vrow},{ref_sheet}!$B$2:$B${n_rows+1},0)),"")'
                )
                c = ws.cell(row=vrow, column=abs_col, value=formula)
                c.font = Font(color="FFFFFF", size=8)

    # ── CGST/SGST auto-fill (col Y=25, Z=26) ─────────────────────────────────
    hsn_id_col = get_column_letter(34)  # AH
    if n_hsn > 0:
        for vrow in range(2, 501):
            cgst_f = (f'=IFERROR(VLOOKUP({hsn_id_col}{vrow},'
                      f'HSN_REF!$A$2:$D${n_hsn+1},3,0),"")')
            sgst_f = (f'=IFERROR(VLOOKUP({hsn_id_col}{vrow},'
                      f'HSN_REF!$A$2:$D${n_hsn+1},4,0),"")')
            for col_i, formula in [(25, cgst_f), (26, sgst_f)]:
                c = ws.cell(row=vrow, column=col_i, value=formula)
                c.fill      = PatternFill("solid", fgColor=C_LOCK_BG)
                c.font      = Font(italic=True, color=C_LOCK_FONT, size=9)
                c.border    = thin
                c.alignment = Alignment(horizontal="center", vertical="center")
                c.protection = openpyxl.styles.Protection(locked=True)

    # ── Sample data ───────────────────────────────────────────────────────────
    today_str = datetime.now().strftime("%Y-%m-%d")
    sample_products = [
        {"productCode":"PRD-001","name":"Product 1","description":"First product description",
         "categoryName":"","brandName":"","childCategoryName":""},
        {"productCode":"PRD-002","name":"Product 2","description":"Second product description",
         "categoryName":"","brandName":"","childCategoryName":""},
    ]
    sample_variants = [
        {"variantCode":"VAR-RED",   "variantName":"Red Variant",   "color":"Red",
         "isBestSeller":"TRUE",  "isReturnAvailable":"TRUE",  "isCupSize":"FALSE",
         "variantDescription":"","fabricName":"","hsnCode":""},
        {"variantCode":"VAR-BLUE",  "variantName":"Blue Variant",  "color":"Blue",
         "isBestSeller":"FALSE", "isReturnAvailable":"TRUE",  "isCupSize":"FALSE",
         "variantDescription":"","fabricName":"","hsnCode":""},
        {"variantCode":"VAR-GREEN", "variantName":"Green Variant", "color":"Green",
         "isBestSeller":"FALSE", "isReturnAvailable":"FALSE", "isCupSize":"FALSE",
         "variantDescription":"","fabricName":"","hsnCode":""},
    ]
    sample_sizes = [
        {"mrpPrice":999,"finalPrice":799,"availableQuantity":50,"discountPercent":20,
         "purchasePrice":500,"cgst":"","sgst":"","storeName":"","warehouseName":"",
         "entryDate":today_str,"stockId":"","sizeName":"","cupSizeName":""},
        {"mrpPrice":999,"finalPrice":799,"availableQuantity":30,"discountPercent":20,
         "purchasePrice":500,"cgst":"","sgst":"","storeName":"","warehouseName":"",
         "entryDate":today_str,"stockId":"","sizeName":"","cupSizeName":""},
    ]
    structure = [
        (sample_products[0], [sample_variants[0], sample_variants[1]], sample_sizes),
        (sample_products[1], [sample_variants[2]], sample_sizes),
    ]

    row_n = 2
    for prod, variants, sizes_list in structure:
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

            for sz in sizes_list:
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
                        c = ws.cell(row=row_n, column=col + 1, value=sz.get(field, ""))
                        c.fill          = PatternFill("solid", fgColor=C_DATE_BG)
                        c.font          = Font(color=C_SIZE_TYP, size=10)
                        c.border        = thin
                        c.alignment     = Alignment(horizontal="center", vertical="center")
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
            col_field = next((f for f, c_idx, _, _ in _S_FIELDS if c_idx == col_i), None)
            c = ws.cell(row=row_n, column=col_i + 1, value="")
            if col_field in ("cgst", "sgst"):
                c.fill = PatternFill("solid", fgColor=C_LOCK_BG)
                c.font = Font(italic=True, color=C_LOCK_FONT, size=9)
            elif col_field == "entryDate":
                c.fill          = PatternFill("solid", fgColor=C_DATE_BG)
                c.number_format = "YYYY-MM-DD"
            c.border = thin
        ws.row_dimensions[row_n].height = 18
        row_n += 1

    # ── Sheet protection: unlock everything first, then lock ONLY CGST/SGST ──
    for row in ws.iter_rows(min_row=1, max_row=5000, min_col=1, max_col=_TOTAL_COLS):
        for cell in row:
            cell.protection = openpyxl.styles.Protection(locked=False)

    for row in ws.iter_rows(min_row=2, max_row=5000, min_col=25, max_col=26):
        for cell in row:
            cell.protection = openpyxl.styles.Protection(locked=True)

    ws.protection.sheet               = True
    ws.protection.password            = "twam_auto"
    ws.protection.selectLockedCells   = False
    ws.protection.selectUnlockedCells = False
    ws.protection.sort                = False
    ws.protection.autoFilter          = False
    ws.protection.insertRows          = False
    ws.protection.deleteRows          = False
    ws.protection.formatCells         = False
    ws.protection.formatColumns       = False
    ws.protection.formatRows          = False

    # ── Instruction note ──────────────────────────────────────────────────────
    note_row = row_n
    note_cell = ws.cell(
        row=note_row, column=27,
        value="⚠ Fill EITHER Store Name OR Warehouse Name (not both). Date is required.",
    )
    note_cell.font      = Font(italic=True, color="92400E", size=9, bold=True)
    note_cell.fill      = PatternFill("solid", fgColor="FEF3C7")
    note_cell.alignment = Alignment(horizontal="left", wrap_text=True)
    ws.merge_cells(start_row=note_row, start_column=27, end_row=note_row, end_column=29)
    ws.row_dimensions[note_row].height = 28

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


# ==============================================================================
#  Parser
# ==============================================================================

def _parse_hierarchical_excel(
    file_bytes: bytes,
    ref_maps: dict | None = None,
) -> list[dict]:
    """
    Parse the hierarchical Excel file into a flat list of SIZE row records.

    ref_maps (optional) — pass to enable DB-lookup validation inside the parser:
      {
        "cat_map":        {name → id},
        "child_cat_map":  {name → id},
        "brand_map":      {name → id},
        "fabric_map":     {name → id},
        "size_map":       {label → id},   ← strip+lower keyed
        "cup_size_map":   {label → id},
        "store_map":      {name → id},
        "warehouse_map":  {name → id},
        "hsn_by_code":    {code → {id,cgst,sgst}},
      }

    When ref_maps is provided every name→id lookup is validated here and
    errors are added to rec["_errors"] so the row is marked invalid and
    skipped — no NULL foreign keys ever reach the DB.
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

    # Build case-insensitive stripped lookup helpers
    def _ci_map(d: dict | None) -> dict:
        """Return a copy with keys lowercased+stripped for fuzzy lookup."""
        if not d:
            return {}
        return {k.strip().lower(): v for k, v in d.items()}

    rm             = ref_maps or {}
    ci_size_map    = _ci_map(rm.get("size_map"))
    ci_cup_map     = _ci_map(rm.get("cup_size_map"))
    ci_store_map   = _ci_map(rm.get("store_map"))
    ci_wh_map      = _ci_map(rm.get("warehouse_map"))
    ci_fabric_map  = _ci_map(rm.get("fabric_map"))
    ci_cat_map     = _ci_map(rm.get("cat_map"))
    ci_child_map   = _ci_map(rm.get("child_cat_map"))
    ci_brand_map   = _ci_map(rm.get("brand_map"))
    hsn_by_code    = rm.get("hsn_by_code") or {}

    def _lookup(ci_d: dict, raw_val: str | None, field_label: str, row_n: int, errors: list, required=False) -> int | None:
        """Case-insensitive strip lookup. Returns id or None. Appends error if required and missing/not-found."""
        if not raw_val or not raw_val.strip():
            if required:
                errors.append(f"Row {row_n}: '{field_label}' is required")
            return None
        key = raw_val.strip().lower()
        val = ci_d.get(key)
        if val is None and ci_d:  # map exists but value not found
            errors.append(f"Row {row_n}: '{field_label}' value '{raw_val.strip()}' not found in database")
        return val

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

            # Read pre-resolved IDs from hidden VLOOKUP columns
            # Trust these over re-doing name→id lookups
            raw_cat_id   = _cell(row, _COL_CATEGORY_ID)
            raw_child_id = _cell(row, _COL_CHILD_CAT_ID)
            raw_brand_id = _cell(row, _COL_BRAND_ID)
            try:
                current_product["_categoryId"]   = int(float(str(raw_cat_id)))   if raw_cat_id   else None
            except (ValueError, TypeError):
                current_product["_categoryId"]   = None
            try:
                current_product["_childCatId"]   = int(float(str(raw_child_id))) if raw_child_id else None
            except (ValueError, TypeError):
                current_product["_childCatId"]   = None
            try:
                current_product["_brandId"]      = int(float(str(raw_brand_id))) if raw_brand_id else None
            except (ValueError, TypeError):
                current_product["_brandId"]      = None

        elif "VARIANT" in raw_type:
            current_variant = {"_row": row_num}
            for field, col, dtype, req in _V_FIELDS:
                try:
                    current_variant[field] = _cast(_cell(row, col), dtype)
                except ValueError:
                    current_variant[field] = None

            # Read pre-resolved IDs from hidden VLOOKUP columns
            raw_fabric_id = _cell(row, _COL_FABRIC_ID)
            raw_hsn_id    = _cell(row, _COL_HSN_ID)
            try:
                current_variant["_fabricId"] = int(float(str(raw_fabric_id))) if raw_fabric_id else None
            except (ValueError, TypeError):
                current_variant["_fabricId"] = None
            try:
                current_variant["_hsnId"]    = int(float(str(raw_hsn_id)))    if raw_hsn_id    else None
            except (ValueError, TypeError):
                current_variant["_hsnId"]    = None

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

            # ── Parse all SIZE fields ─────────────────────────────────────────
            for field, col, dtype, req in _S_FIELDS:
                raw = _cell(row, col)
                try:
                    val = _cast(raw, dtype)
                except ValueError:
                    errors.append(f"Row {row_num}: '{field}' has invalid value '{raw}'")
                    val = None
                if req and field not in ("storeName", "warehouseName") and val is None:
                    errors.append(f"Row {row_num}: '{field}' is required")
                rec[field] = val

            # ── Store / Warehouse: prefer hidden VLOOKUP col, fall back to name lookup ──
            # col AI (34) = StoreId already resolved by template VLOOKUP
            # col AJ (35) = WarehouseId already resolved by template VLOOKUP
            raw_store_id = _cell(row, _COL_STORE_ID)
            raw_wh_id    = _cell(row, _COL_WAREHOUSE_ID)

            try:
                hidden_store_id = int(float(str(raw_store_id))) if raw_store_id else None
            except (ValueError, TypeError):
                hidden_store_id = None
            try:
                hidden_wh_id = int(float(str(raw_wh_id))) if raw_wh_id else None
            except (ValueError, TypeError):
                hidden_wh_id = None

            store_val = (rec.get("storeName") or "").strip()
            wh_val    = (rec.get("warehouseName") or "").strip()

            if store_val and wh_val:
                errors.append(f"Row {row_num}: Provide EITHER Store Name OR Warehouse Name, not both.")
                rec["storeId"]     = None
                rec["warehouseId"] = None
            elif not store_val and not wh_val and hidden_store_id is None and hidden_wh_id is None:
                errors.append(f"Row {row_num}: Either Store Name or Warehouse Name is required.")
                rec["storeId"]     = None
                rec["warehouseId"] = None
            elif store_val or hidden_store_id is not None:
                # Prefer hidden VLOOKUP ID; fall back to name map
                sid = hidden_store_id or ci_store_map.get(store_val.lower()) if store_val else hidden_store_id
                if sid is None and store_val and ci_store_map:
                    errors.append(f"Row {row_num}: Store '{store_val}' not found in database")
                elif sid is None and not store_val:
                    errors.append(f"Row {row_num}: Either Store Name or Warehouse Name is required.")
                rec["storeId"]     = sid
                rec["warehouseId"] = None
            else:  # warehouse
                wid = hidden_wh_id or ci_wh_map.get(wh_val.lower()) if wh_val else hidden_wh_id
                if wid is None and wh_val and ci_wh_map:
                    errors.append(f"Row {row_num}: Warehouse '{wh_val}' not found in database")
                elif wid is None and not wh_val:
                    errors.append(f"Row {row_num}: Either Store Name or Warehouse Name is required.")
                rec["storeId"]     = None
                rec["warehouseId"] = wid

            # ── Size: prefer hidden VLOOKUP col AK (36), fall back to name lookup ──
            raw_size_id = _cell(row, _COL_SIZE_ID)
            try:
                hidden_size_id = int(float(str(raw_size_id))) if raw_size_id else None
            except (ValueError, TypeError):
                hidden_size_id = None

            size_label = (rec.get("sizeName") or "").strip()
            if hidden_size_id is not None:
                rec["sizeId"] = hidden_size_id         # trust the template VLOOKUP
            elif size_label:
                sid = ci_size_map.get(size_label.lower())
                if sid is None and ci_size_map:
                    errors.append(f"Row {row_num}: Size '{size_label}' not found in database — check the Size dropdown")
                rec["sizeId"] = sid
            else:
                errors.append(f"Row {row_num}: Size is required")
                rec["sizeId"] = None

            # ── Cup Size: prefer hidden VLOOKUP col AL (37) ───────────────────
            raw_cup_id = _cell(row, _COL_CUP_SIZE_ID)
            try:
                hidden_cup_id = int(float(str(raw_cup_id))) if raw_cup_id else None
            except (ValueError, TypeError):
                hidden_cup_id = None

            cup_label = (rec.get("cupSizeName") or "").strip()
            if hidden_cup_id is not None:
                rec["cupSizeId"] = hidden_cup_id
            elif cup_label:
                cid = ci_cup_map.get(cup_label.lower())
                if cid is None and ci_cup_map:
                    errors.append(f"Row {row_num}: Cup Size '{cup_label}' not found in database")
                rec["cupSizeId"] = cid
            else:
                rec["cupSizeId"] = None

            # ── Category / Brand / Fabric: prefer hidden cols, fall back to name ──
            # Category: hidden col AD (29)
            if current_product.get("_categoryId") is not None:
                rec["categoryId"] = current_product["_categoryId"]
            elif ci_cat_map:
                cat_name = (current_product.get("categoryName") or "").strip()
                rec["categoryId"] = ci_cat_map.get(cat_name.lower()) if cat_name else None
                if cat_name and rec.get("categoryId") is None:
                    errors.append(f"Row {current_product.get('_row','?')}: Category '{cat_name}' not found in database")
            else:
                rec["categoryId"] = None

            # Child category: hidden col AE (30)
            if current_product.get("_childCatId") is not None:
                rec["childCategoryId"] = current_product["_childCatId"]
            elif ci_child_map:
                child_name = (current_product.get("childCategoryName") or "").strip()
                rec["childCategoryId"] = ci_child_map.get(child_name.lower()) if child_name else None
            else:
                rec["childCategoryId"] = None

            # Brand: hidden col AF (31)
            if current_product.get("_brandId") is not None:
                rec["brandId"] = current_product["_brandId"]
            elif ci_brand_map:
                brand_name = (current_product.get("brandName") or "").strip()
                rec["brandId"] = ci_brand_map.get(brand_name.lower()) if brand_name else None
                if brand_name and rec.get("brandId") is None:
                    errors.append(f"Row {current_product.get('_row','?')}: Brand '{brand_name}' not found in database")
            else:
                rec["brandId"] = None

            # Fabric: hidden col AG (32)
            if current_variant.get("_fabricId") is not None:
                rec["fabricId"] = current_variant["_fabricId"]
            elif ci_fabric_map:
                fabric_name = (current_variant.get("fabricName") or "").strip()
                rec["fabricId"] = ci_fabric_map.get(fabric_name.lower()) if fabric_name else None
            else:
                rec["fabricId"] = None

            # HSN: hidden col AH (33)
            if current_variant.get("_hsnId") is not None:
                hsn_id = current_variant["_hsnId"]
                rec["variantTaxHSNCodeId"] = hsn_id
                # Look up CGST/SGST from hsn_by_code using the code string
                hsn_code = (current_variant.get("hsnCode") or "").strip()
                hsn_entry = hsn_by_code.get(hsn_code) if hsn_code else None
                rec["_cgst"] = hsn_entry["cgst"] if hsn_entry else None
                rec["_sgst"] = hsn_entry["sgst"] if hsn_entry else None
            else:
                hsn_code  = (current_variant.get("hsnCode") or "").strip()
                hsn_entry = hsn_by_code.get(hsn_code) if hsn_code else None
                rec["variantTaxHSNCodeId"] = hsn_entry["id"]   if hsn_entry else None
                rec["_cgst"]               = hsn_entry["cgst"] if hsn_entry else None
                rec["_sgst"]               = hsn_entry["sgst"] if hsn_entry else None

            # ── Date ─────────────────────────────────────────────────────────
            if rec.get("entryDate") is None:
                errors.append(f"Row {row_num}: Date is required.")

            # ── Required product fields ───────────────────────────────────────
            _P_REQUIRED = {
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
                        errors.append(
                            f"Row {current_product.get('_row','?')}: "
                            f"Product field '{_P_REQUIRED.get(field,field)}' is required"
                        )

            # ── Required variant fields ───────────────────────────────────────
            _V_REQUIRED = {
                "variantCode": "Product Code",
                "variantName": "Variant Name",
                "color":       "Color",
                "isCupSize":   "Is Cup Size",
            }
            for field, col, dtype, req in _V_FIELDS:
                if req:
                    val = current_variant.get(field)
                    if val is None or (isinstance(val, str) and not val.strip()):
                        errors.append(
                            f"Row {current_variant.get('_row','?')}: "
                            f"Variant field '{_V_REQUIRED.get(field,field)}' is required"
                        )

            # ── Cup size cross-validation ─────────────────────────────────────
            is_cup = current_variant.get("isCupSize")
            if isinstance(is_cup, str):
                is_cup = is_cup.strip().upper() == "TRUE"
            elif is_cup is None:
                is_cup = False
            if is_cup and not cup_label:
                errors.append(f"Row {row_num}: Is Cup Size = TRUE but Cup Size is missing")
            if not is_cup and cup_label:
                errors.append(f"Row {row_num}: Is Cup Size = FALSE but Cup Size should not be provided")

            # ── MRP vs Final Price ────────────────────────────────────────────
            mrp   = rec.get("mrpPrice")
            final = rec.get("finalPrice")
            if mrp is not None and final is not None and final > mrp:
                errors.append(f"Row {row_num}: Final Price ({final}) cannot exceed MRP ({mrp})")

            stock = rec.get("availableQuantity") or 0
            rec["isOutOfStock"] = stock == 0
            rec["isLowStock"]   = not rec["isOutOfStock"] and stock < 10
            rec["_errors"]      = errors
            rec["_valid"]       = len(errors) == 0
            size_records.append(rec)

    return size_records


def _build_hierarchical_chains(size_records: list[dict]) -> dict[str, dict]:
    """
    Group valid SIZE records into product→variant→slots hierarchy.
    All ID lookups already resolved in _parse_hierarchical_excel.

    Key fix: pkey uses p_row (line number) as the primary identifier,
    NOT productCode. This means products with code 'NA' or duplicate
    codes are correctly treated as separate products.
    """
    chains: dict[str, dict] = {}

    for rec in size_records:
        if not rec["_valid"] or _is_sample_row(rec):
            continue

        pcode = rec.get("productCode") or ""
        vcode = rec.get("variantCode") or ""

        p_row = rec.get("_p_row")
        v_row = rec.get("_v_row")

        # Use row number as primary key — product name as secondary to be safe
        # This ensures 'NA' product codes don't all collapse into one product
        p_name = rec.get("name") or ""
        pkey   = f"p_row_{p_row}_{p_name}"          # row-based → unique per PRODUCT row
        vkey   = f"v_row_{v_row}_{vcode}_{rec.get('color','')}"  # row+color for uniqueness

        if pkey not in chains:
            chains[pkey] = {
                "rec": {
                    "productCode":     pcode if pcode.upper() != "NA" else None,
                    "name":            rec.get("name"),
                    "description":     rec.get("description"),
                    "categoryId":      rec.get("categoryId"),
                    "childCategoryId": rec.get("childCategoryId"),
                    "brandId":         rec.get("brandId"),
                    "tag":             rec.get("tag"),
                    # FIX: Propagate HSN code to product level so it is stored in
                    # twam.Products.TaxHSNCodeId — compute_tax_for_product reads from
                    # Products, not ProductVariants, so this is the authoritative source.
                    "taxHSNCodeId":    rec.get("variantTaxHSNCodeId"),
                },
                "variants": {},
            }

        if vkey not in chains[pkey]["variants"]:
            chains[pkey]["variants"][vkey] = {
                "rec": {
                    "variantCode":         vcode if vcode.upper() != "NA" else None,
                    "variantName":         rec.get("variantName"),
                    "variantDescription":  rec.get("variantDescription"),
                    "fabricId":            rec.get("fabricId"),
                    "variantTaxHSNCodeId": rec.get("variantTaxHSNCodeId"),
                    "color":               rec.get("color"),
                    "isBestSeller":        rec.get("isBestSeller") or False,
                    "isReturnAvailable":   rec.get("isReturnAvailable") or False,
                    "isCupSize":           rec.get("isCupSize") or False,
                    "productCode":         pcode,   # for warnings/logging
                },
                "slots": [],
            }

        cgst = rec.get("cgst") or rec.get("_cgst")
        sgst = rec.get("sgst") or rec.get("_sgst")

        chains[pkey]["variants"][vkey]["slots"].append({
            "sizeId":            rec.get("sizeId"),
            "cupSizeId":         rec.get("cupSizeId"),
            "stockId":           rec.get("stockId"),
            "storeId":           rec.get("storeId"),
            "warehouseId":       rec.get("warehouseId"),
            "storeName":         rec.get("storeName"),
            "warehouseName":     rec.get("warehouseName"),
            "mrpPrice":          rec["mrpPrice"],
            "finalPrice":        rec["finalPrice"],
            "availableQuantity": rec["availableQuantity"],
            "discountPercent":   rec.get("discountPercent") or 0,
            "purchasePrice":     rec.get("purchasePrice"),
            "cgst":              cgst,
            "sgst":              sgst,
            "entryDate":         rec.get("entryDate"),
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


def _apply_hsn_rates(chains: dict, hsn_map: dict[int, dict], db=None) -> list[dict]:
    """
    Compute CGST/SGST for every slot using the parent product's TaxHSNCode
    and the slot's finalPrice.  When a price-slab DB lookup is available
    (db is passed) we use it; otherwise we fall back to the flat hsn_map rates.
    """
    warnings: list[dict] = []
    seen_missing: set[int] = set()
    for chain in chains.values():
        product_id = chain.get("productId")
        for var in chain["variants"]:
            hsn_id = var["rec"].get("variantTaxHSNCodeId")
            if not hsn_id:
                continue
            hsn_id = int(hsn_id)
            for slot in var["slots"]:
                final_price = float(slot.get("finalPrice") or 0)
                # Prefer live price-slab lookup when DB session available
                if db is not None and product_id:
                    try:
                        from app.taxhsncode.repository import compute_tax_for_product
                        tax = compute_tax_for_product(db, product_id, final_price)
                        slot["cgst"]      = tax["cgstRate"]
                        slot["sgst"]      = tax["sgstRate"]
                        slot["taxAmount"] = tax["taxAmount"]
                        continue
                    except Exception:
                        pass  # fall through to flat rate
                # Flat-rate fallback from hsn_map
                rates = hsn_map.get(hsn_id)
                if rates:
                    slot["cgst"]      = round(final_price * rates["cgst"] / 100, 2) if final_price else rates["cgst"]
                    slot["sgst"]      = round(final_price * rates["sgst"] / 100, 2) if final_price else rates["sgst"]
                    slot["taxAmount"] = round((slot["cgst"] + slot["sgst"]), 2)
                elif hsn_id not in seen_missing:
                    seen_missing.add(hsn_id)
                    warnings.append({
                        "hsnCodeId":   hsn_id,
                        "variantCode": var["rec"].get("variantCode"),
                        "message":     f"TaxHSNCodeId {hsn_id} not found in DB — CGST/SGST will be NULL.",
                    })
    return warnings


def _resolve_stores_warehouses(
    chains: dict,
    store_map: dict[str, int],
    warehouse_map: dict[str, int],
) -> tuple[list[dict], list[dict]]:
    """Resolve storeName→storeId and warehouseName→warehouseId for all slots."""
    store_warnings:     list[dict] = []
    warehouse_warnings: list[dict] = []
    seen_bad_stores:    set[str]   = set()
    seen_bad_wh:        set[str]   = set()

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
                    if not wid and wname not in seen_bad_wh:
                        seen_bad_wh.add(wname)
                        warehouse_warnings.append({
                            "warehouseName": wname,
                            "variantCode":   var["rec"].get("variantCode"),
                            "message":       f"Warehouse '{wname}' not found in DB — WarehouseId will be NULL.",
                        })

    return store_warnings, warehouse_warnings


def _check_duplicate_stock_ids(
    chains: dict,
    size_records: list[dict],
    db: Session,
    remove_duplicates: bool = False,
) -> tuple[dict, list[dict]]:
    """
    Check ALL SIZE rows (valid or not) for stockIds that already exist in DB.

    Key fix: marks _is_duplicate on the ORIGINAL size_records objects so the
    preview table can read it directly. Previously only chain slots (copies)
    were marked, which never propagated back to size_records.

    If remove_duplicates=True, also removes those slots from chains (import mode).
    Always returns (chains, duplicate_warnings).
    """
    # Collect stock IDs from ALL rows — valid or not — so even error rows
    # are checked and shown as duplicates in preview
    all_stock_ids: list[int] = []
    for rec in size_records:
        raw = rec.get("stockId")
        if raw is not None:
            try:
                all_stock_ids.append(int(raw))
            except (ValueError, TypeError):
                pass

    existing: set[int] = _fetch_existing_stock_ids(db, all_stock_ids) if all_stock_ids else set()

    # ── Step 1: mark original size_records directly ───────────────────────────
    # This is what the preview table reads via rec.get("_is_duplicate")
    duplicate_warnings: list[dict] = []
    seen_dup: set[int] = set()

    for rec in size_records:
        raw = rec.get("stockId")
        if raw is None:
            rec["_is_duplicate"] = False
            continue
        try:
            sid = int(raw)
        except (ValueError, TypeError):
            rec["_is_duplicate"] = False
            continue

        if sid in existing:
            rec["_is_duplicate"] = True
            if sid not in seen_dup:
                seen_dup.add(sid)
                duplicate_warnings.append({
                    "stockId":     str(sid),
                    "productCode": rec.get("productCode"),
                    "variantCode": rec.get("variantCode"),
                    "message": (
                        f"⚠ Stock ID '{sid}' already exists in the database. "
                        f"This row was skipped. Please use a different Stock ID."
                    ),
                })
        else:
            rec["_is_duplicate"] = False

    # ── Step 2: handle chains (for import removal) ────────────────────────────
    for chain in chains.values():
        for var in chain["variants"]:
            clean_slots = []
            for slot in var["slots"]:
                raw_sid = slot.get("stockId")
                if raw_sid is None:
                    clean_slots.append(slot)
                    continue
                try:
                    sid = int(raw_sid)
                except (ValueError, TypeError):
                    clean_slots.append(slot)
                    continue

                if sid in existing:
                    if not remove_duplicates:
                        clean_slots.append(slot)  # keep in preview
                    # else: drop from import
                else:
                    clean_slots.append(slot)
            var["slots"] = clean_slots

    if remove_duplicates:
        pruned: dict[str, dict] = {}
        for pk, ch in chains.items():
            good_variants = [v for v in ch["variants"] if v["slots"]]
            if good_variants:
                pruned[pk] = {"rec": ch["rec"], "variants": good_variants}
        chains = pruned

    return chains, duplicate_warnings


# ==============================================================================
#  SQL inserts
# ==============================================================================

def _pb_insert_product(db: Session, rec: dict) -> int:
    # FIX: Include TaxHSNCodeId in Products INSERT so that compute_tax_for_product
    # (which reads from twam.Products.TaxHSNCodeId) works for this product's PVDs.
    r = db.execute(text("""
        INSERT INTO twam."Products" (
            "ProductCode","Name","Description",
            "CategoryId","ChildCategoryId","BrandId","Tag",
            "TaxHSNCodeId",
            "State","CreatedDate","DeletedInd"
        ) VALUES (
            :productCode,:name,:description,
            :categoryId,:childCategoryId,:brandId,:tag,
            :taxHSNCodeId,
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
        "taxHSNCodeId":    rec.get("taxHSNCodeId"),
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


def _pb_insert_detail(db: Session, slot: dict, product_id: int, variant_id: int, variant_code: str) -> int:
    r = db.execute(text("""
        INSERT INTO twam."ProductVariantDetail" (
            "ProductId","ProductVariantId","ProductCode",
            "Size","CupSize","StockId",
            "PurchasePrice","AvailableQuantity",
            "ProcessedQuantity","ReturnedQuantity","AmendmentQuantity",
            "MRPPrice","FinalPrice","DiscountPercent",
            "CGST","SGST","StoreId","WarehouseId","Date",
            "IsLowStock","IsOutOfStock",
            "State","CreatedDate","DeletedInd"
        ) VALUES (
            :productId,:productVariantId,:productCode,
            :size,:cupSize,:stockId,
            :purchasePrice,:availableQuantity,
            0,0,0,
            :mrpPrice,:finalPrice,:discountPercent,
            :cgst,:sgst,:storeId,:warehouseId,:entryDate,
            :isLowStock,:isOutOfStock,
            'Created',:now,false
        ) RETURNING "ProductVariantDetailId"
    """), {
        "productId":         product_id,
        "productVariantId":  variant_id,
        "productCode":       str(variant_code),
        "size":              int(slot["sizeId"])        if slot.get("sizeId")        is not None else None,
        "cupSize":           int(slot["cupSizeId"])     if slot.get("cupSizeId")     is not None else None,
        "stockId":           int(slot["stockId"])       if slot.get("stockId")       is not None else None,
        "purchasePrice":     int(slot["purchasePrice"]) if slot.get("purchasePrice") is not None else None,
        "availableQuantity": int(slot["availableQuantity"]),
        "mrpPrice":          float(slot["mrpPrice"]),
        "finalPrice":        float(slot["finalPrice"]),
        "discountPercent":   int(slot.get("discountPercent") or 0),
        "cgst":              float(slot["cgst"])      if slot.get("cgst")        is not None else None,
        "sgst":              float(slot["sgst"])      if slot.get("sgst")        is not None else None,
        "storeId":           int(slot["storeId"])     if slot.get("storeId")     is not None else None,
        "warehouseId":       int(slot["warehouseId"]) if slot.get("warehouseId") is not None else None,
        "entryDate":         slot.get("entryDate"),
        "isLowStock":        bool(slot.get("isLowStock",   False)),
        "isOutOfStock":      bool(slot.get("isOutOfStock", False)),
        "now":               NOW(),
    })
    return r.scalar_one()


# ==============================================================================
#  ROUTES — Generic entities
# ==============================================================================

@router.get("/entities")
def list_entities():
    return {
        "entities": [
            {"key": k, "label": k.replace("_"," ").title(),
             "columns": [c[1] for c in v["columns"]],
             "required": v["required"], "notes": v["notes"]}
            for k, v in ENTITY_CONFIG.items()
        ]
    }


@router.get("/template/{entity}")
def download_entity_template(entity: str):
    _check_openpyxl()
    if entity not in ENTITY_CONFIG:
        raise HTTPException(status_code=404, detail=f"Entity '{entity}' not supported.")
    cfg = ENTITY_CONFIG[entity]; col_defs = cfg["columns"]; sample = cfg.get("sample", {})
    wb = Workbook(); ws = wb.active; ws.title = entity.title()
    border = _thin_border(); center = _center_align()
    hdr_font = Font(bold=True, color="FFFFFF", size=11)
    hdr_fill = PatternFill("solid", fgColor="6B0F2A")
    req_fill = PatternFill("solid", fgColor="F5E6EA")
    for col_idx, (field, label, dtype, req) in enumerate(col_defs, start=1):
        c = ws.cell(row=1, column=col_idx, value=label)
        c.font = hdr_font; c.fill = hdr_fill; c.alignment = center; c.border = border
        ws.column_dimensions[get_column_letter(col_idx)].width = max(20, len(label) + 4)
    for col_idx, (field, label, dtype, req) in enumerate(col_defs, start=1):
        c = ws.cell(row=2, column=col_idx, value=sample.get(field, ""))
        c.fill = req_fill if req else PatternFill("solid", fgColor="FFFFFF"); c.border = border
    ws.cell(row=4, column=1, value="Notes:").font = Font(bold=True, size=10)
    for i, note in enumerate([cfg["notes"], "Columns marked with * are required.",
                               "Delete row 2 (sample) before uploading."], start=5):
        ws.cell(row=i, column=1, value=f"* {note}").font = Font(italic=True, color="666666", size=9)
    ws.freeze_panes = "A2"; ws.row_dimensions[1].height = 30
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return StreamingResponse(buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="twam_{entity}_template.xlsx"'})


@router.post("/preview/{entity}")
async def preview_entity(entity: str, file: UploadFile = File(...)):
    if entity not in ENTITY_CONFIG:
        raise HTTPException(status_code=404, detail=f"Entity '{entity}' not supported.")
    content = await file.read()
    rows    = _parse_entity_excel(content, entity)
    return {
        "entity": entity, "totalRows": len(rows),
        "validRows": sum(1 for r in rows if not r["_errors"]),
        "invalidRows": sum(1 for r in rows if r["_errors"]),
        "columns": [c[1] for c in ENTITY_CONFIG[entity]["columns"]],
        "rows": rows[:200],
        "canImport": any(not r["_errors"] for r in rows),
    }


@router.post("/import/{entity}")
async def import_entity(
    entity: str, file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    if entity not in ENTITY_CONFIG:
        raise HTTPException(status_code=404, detail=f"Entity '{entity}' not supported.")
    if current_user.role_id not in {Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER, Roles.INVENTORY_MANAGER}:
        raise HTTPException(status_code=403, detail="Insufficient permissions.")
    content = await file.read(); rows = _parse_entity_excel(content, entity)
    now = NOW(); inserted = skipped = 0; row_errors: list[dict] = []
    for row in rows:
        if row["_errors"]:
            skipped += 1; row_errors.append({"row": row["_row"], "errors": row["_errors"]}); continue
        try:
            _insert_entity_row(db, entity, row, current_user.user_id, now); inserted += 1
        except Exception as exc:
            db.rollback(); skipped += 1; row_errors.append({"row": row["_row"], "errors": [str(exc)]})
    try:
        db.commit()
    except Exception as exc:
        db.rollback(); raise HTTPException(status_code=500, detail=f"Commit failed: {exc}")
    return {"entity": entity, "inserted": inserted, "skipped": skipped,
            "errors": row_errors[:50],
            "message": f"Successfully imported {inserted} {entity}. {skipped} row(s) skipped."}


# ==============================================================================
#  ROUTE — HSN Tax Info
# ==============================================================================

@router.get("/hsn-tax-info")
async def get_hsn_tax_info(
    codes: str = Query(..., description="Comma-separated TaxHSNCodeIds"),
    db: Session = Depends(get_db),
):
    try:
        ids = [int(x.strip()) for x in codes.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="'codes' must be comma-separated integers.")
    hsn_map = await _fetch_hsn_rates(db, ids) if ids else {}
    found   = {k: v for k, v in hsn_map.items() if k in ids}
    missing = [i for i in ids if i not in found]
    return {"found": found, "missing": missing,
            "message": f"{len(missing)} HSN code(s) not found in DB." if missing else "All HSN codes resolved."}


# ==============================================================================
#  ROUTES — Product Bulk
# ==============================================================================

@router.get("/stores")
async def list_stores(db: Session = Depends(get_db)):
    stores = _fetch_all_stores(db)
    return {"stores": stores, "total": len(stores)}


@router.get("/warehouses")
async def list_warehouses(db: Session = Depends(get_db)):
    warehouses = _fetch_all_warehouses(db)
    return {"warehouses": warehouses, "total": len(warehouses)}


@router.get("/product-bulk/template")
def download_product_bulk_template(db: Session = Depends(get_db)):
    stores     = _fetch_all_stores(db)
    warehouses = _fetch_all_warehouses(db)
    refs       = _fetch_ref_data(db)
    xlsx_bytes = _build_hierarchical_template(stores, refs, warehouses)
    return StreamingResponse(io.BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="twam_product_bulk_template.xlsx"'})


@router.post("/product-bulk/preview")
async def preview_product_bulk(file: UploadFile = File(...), db: Session = Depends(get_db)):
    content = await file.read()

    # ── Fetch all reference data once ────────────────────────────────────────
    refs          = _fetch_ref_data(db)
    store_map     = _fetch_store_map(db)
    warehouse_map = _fetch_warehouse_map(db)

    ref_maps = {
        "cat_map":       {r["name"]: r["id"] for r in refs["categories"]},
        "child_cat_map": {r["name"]: r["id"] for r in refs["childCategories"]},
        "brand_map":     {r["name"]: r["id"] for r in refs["brands"]},
        "fabric_map":    {r["name"]: r["id"] for r in refs["fabrics"]},
        "size_map":      {r["label"]: r["id"] for r in refs["sizes"]},
        "cup_size_map":  {r["label"]: r["id"] for r in refs["cupSizes"]},
        "store_map":     store_map,
        "warehouse_map": warehouse_map,
        "hsn_by_code":   {r["code"]: r for r in refs["hsnCodes"]},
    }

    # ── Parse — all lookups and validations done here ─────────────────────────
    size_records = _parse_hierarchical_excel(content, ref_maps=ref_maps)

    # ── Build chains from valid records ───────────────────────────────────────
    chains = _build_hierarchical_chains(size_records)

    # ── Apply HSN CGST/SGST rates ─────────────────────────────────────────────
    hsn_ids      = _collect_hsn_ids(chains)
    hsn_map      = await _fetch_hsn_rates(db, hsn_ids)
    hsn_warnings = _apply_hsn_rates(chains, hsn_map)

    # ── Duplicate stock ID check (mark only, don't remove) ───────────────────
    chains, duplicate_stock_warnings = _check_duplicate_stock_ids(
        chains, size_records, db, remove_duplicates=False
    )

    # ── Build preview rows ────────────────────────────────────────────────────
    preview_rows = []
    for rec in size_records:
        preview_rows.append({
            "row":              rec["_row"],
            "status":           "Duplicate" if rec.get("_is_duplicate") else ("OK" if rec["_valid"] else "Error"),
            "issues":           rec["_errors"],
            "productCode":      rec.get("productCode"),
            "productName":      rec.get("name"),
            "variantCode":      rec.get("variantCode"),
            "variantName":      rec.get("variantName"),
            "fabricName":       rec.get("fabricName"),
            "hsnCode":          rec.get("hsnCode"),
            "stockId":          rec.get("stockId"),
            "color":            rec.get("color"),
            "sizeName":         rec.get("sizeName"),
            "sizeId":           rec.get("sizeId"),
            "storeId":          rec.get("storeId"),
            "warehouseId":      rec.get("warehouseId"),
            "storeName":        rec.get("storeName"),
            "warehouseName":    rec.get("warehouseName"),
            "mrpPrice":         rec.get("mrpPrice"),
            "finalPrice":       rec.get("finalPrice"),
            "purchasePrice":    rec.get("purchasePrice"),
            "stockQty":         rec.get("availableQuantity"),
            "cgst":             rec.get("cgst") or rec.get("_cgst"),
            "sgst":             rec.get("sgst") or rec.get("_sgst"),
            "entryDate":        str(rec["entryDate"]) if rec.get("entryDate") else None,
            "isLowStock":       rec.get("isLowStock", False),
            "isOutOfStock":     rec.get("isOutOfStock", False),
            "isDuplicateStock": rec.get("_is_duplicate", False),
        })

    n_duplicate = sum(1 for r in size_records if r.get("_is_duplicate"))
    n_error     = sum(1 for r in size_records if not r["_valid"] and not r.get("_is_duplicate"))
    n_valid     = sum(1 for r in size_records if r["_valid"] and not r.get("_is_duplicate"))

    return {
        "totalSizeRows":          len(size_records),
        "validRows":              n_valid,
        "errorRows":              n_error,
        "duplicateStockRows":     n_duplicate,
        "willImportProducts":     len(chains),
        "willImportVariants":     sum(len(v["variants"]) for v in chains.values()),
        "willImportDetails":      sum(len(var["slots"]) for v in chains.values() for var in v["variants"]),
        "canImport":              len(chains) > 0 and n_valid > 0,
        "hsnWarnings":            hsn_warnings,
        "duplicateStockWarnings": duplicate_stock_warnings,
        "rows":                   preview_rows[:500],
    }


@router.post("/product-bulk/import")
async def import_product_bulk(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    if current_user.role_id not in {Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER, Roles.INVENTORY_MANAGER}:
        raise HTTPException(status_code=403, detail="Insufficient permissions.")

    content = await file.read()

    # ── Fetch all reference data once ────────────────────────────────────────
    refs          = _fetch_ref_data(db)
    store_map     = _fetch_store_map(db)
    warehouse_map = _fetch_warehouse_map(db)

    ref_maps = {
        "cat_map":       {r["name"]: r["id"] for r in refs["categories"]},
        "child_cat_map": {r["name"]: r["id"] for r in refs["childCategories"]},
        "brand_map":     {r["name"]: r["id"] for r in refs["brands"]},
        "fabric_map":    {r["name"]: r["id"] for r in refs["fabrics"]},
        "size_map":      {r["label"]: r["id"] for r in refs["sizes"]},
        "cup_size_map":  {r["label"]: r["id"] for r in refs["cupSizes"]},
        "store_map":     store_map,
        "warehouse_map": warehouse_map,
        "hsn_by_code":   {r["code"]: r for r in refs["hsnCodes"]},
    }

    # ── Parse — all lookups and validations done here ─────────────────────────
    size_records = _parse_hierarchical_excel(content, ref_maps=ref_maps)

    # ── Build chains from valid records ───────────────────────────────────────
    chains = _build_hierarchical_chains(size_records)

    # ── Apply HSN CGST/SGST rates ─────────────────────────────────────────────
    hsn_ids      = _collect_hsn_ids(chains)
    hsn_map      = await _fetch_hsn_rates(db, hsn_ids)
    hsn_warnings = _apply_hsn_rates(chains, hsn_map)

    # ── Duplicate check: remove duplicates before insert ─────────────────────
    chains, duplicate_stock_warnings = _check_duplicate_stock_ids(
        chains, size_records, db, remove_duplicates=True
    )

    skipped_validation = sum(1 for r in size_records if not r["_valid"])
    skipped_duplicates = len(duplicate_stock_warnings)
    total_skipped      = skipped_validation + skipped_duplicates

    if not chains:
        return {
            "message": (
                f"No rows imported. "
                f"{skipped_validation} row(s) failed validation. "
                f"{skipped_duplicates} row(s) skipped — Stock IDs already exist in DB."
            ),
            "insertedProducts":       0,
            "insertedVariants":       0,
            "insertedDetails":        0,
            "skippedRows":            total_skipped,
            "skippedValidation":      skipped_validation,
            "skippedDuplicates":      skipped_duplicates,
            "hsnWarnings":            hsn_warnings,
            "duplicateStockWarnings": duplicate_stock_warnings,
        }

    # ── DB Insert ─────────────────────────────────────────────────────────────
    # IMPORTANT: per-slot IntegrityError uses db.begin_nested() (SAVEPOINT)
    # so only that one slot is rolled back — all other inserts stay intact.
    # db.rollback() would wipe ALL prior inserts in the transaction.
    inserted_products = inserted_variants = inserted_details = 0
    runtime_dup_warnings: list[dict] = []

    # Import audit helper once — safe even if stocklog module is unavailable
    try:
        from app.stocklog.repository import add_audit_event as _add_audit
    except Exception:
        _add_audit = None

    _bulk_actor = getattr(current_user, "username", None) or getattr(current_user, "user_name", None) or "BulkUpload"

    try:
        for pcode, chain in chains.items():
            product_id = _pb_insert_product(db, chain["rec"])
            inserted_products += 1

            # FIX: Recompute CGST/SGST for every slot using the REAL product_id
            # now that the product row exists. _apply_hsn_rates() called earlier
            # had product_id=None so it fell back to flat Slab1 rates only.
            # We now call compute_tax_for_product() per slot for correct slab matching.
            _hsn_id = chain["rec"].get("taxHSNCodeId")
            if _hsn_id and product_id:
                try:
                    from app.taxhsncode.repository import compute_tax_for_product
                    for _var in chain["variants"]:
                        for _slot in _var["slots"]:
                            _fp = float(_slot.get("finalPrice") or 0)
                            _tax = compute_tax_for_product(db, product_id, _fp)
                            _slot["cgst"]      = _tax["cgstRate"]
                            _slot["sgst"]      = _tax["sgstRate"]
                            _slot["taxAmount"] = _tax["taxAmount"]
                except Exception as _tex:
                    logger.warning(f"Post-insert tax recompute failed for product {product_id}: {_tex}")

            # Audit: product created via bulk upload
            if _add_audit:
                try:
                    _add_audit(
                        db,
                        product_id   = product_id,
                        event_type   = "PRODUCT_CREATED",
                        event_source = "BulkUpload",
                        description  = f"Product created via bulk upload: {chain['rec'].get('name', '')} [{chain['rec'].get('productCode', '')}]",
                        changed_by   = _bulk_actor,
                        product_code = chain["rec"].get("productCode"),
                        product_name = chain["rec"].get("name"),
                    )
                except Exception:
                    pass

            for var in chain["variants"]:
                variant_id = _pb_insert_variant(db, var["rec"], product_id)
                inserted_variants += 1

                # Audit: variant created via bulk upload
                if _add_audit:
                    try:
                        _add_audit(
                            db,
                            product_id         = product_id,
                            product_variant_id = variant_id,
                            event_type         = "VARIANT_ADDED",
                            event_source       = "BulkUpload",
                            description        = f"Variant added via bulk upload: {var['rec'].get('variantName', '')} [{var['rec'].get('color', '')}]",
                            changed_by         = _bulk_actor,
                            product_code       = var["rec"].get("variantCode") or chain["rec"].get("productCode"),
                            variant_name       = var["rec"].get("variantName"),
                        )
                    except Exception:
                        pass

                for slot in var["slots"]:
                    # Use SAVEPOINT so only THIS slot rolls back on duplicate,
                    # not the entire transaction
                    try:
                        sp = db.begin_nested()          # SAVEPOINT
                        pvd_id = _pb_insert_detail(db, slot, product_id, variant_id, var["rec"]["variantCode"])
                        sp.commit()                     # RELEASE SAVEPOINT
                        inserted_details += 1

                        # Audit: PVD (size/detail) created via bulk upload
                        if _add_audit:
                            try:
                                _add_audit(
                                    db,
                                    product_id               = product_id,
                                    product_variant_id       = variant_id,
                                    product_variant_detail_id = pvd_id,
                                    event_type               = "SIZE_ADDED",
                                    event_source             = "BulkUpload",
                                    description              = f"Size added via bulk upload — stock: {slot.get('availableQuantity', 0)}",
                                    stock_before             = None,
                                    stock_after              = slot.get("availableQuantity"),
                                    changed_by               = _bulk_actor,
                                    product_code             = var["rec"].get("variantCode"),
                                    stock_id                 = str(slot["stockId"]) if slot.get("stockId") is not None else None,
                                )
                            except Exception:
                                pass

                    except IntegrityError:
                        sp.rollback()                   # rolls back SAVEPOINT only
                        sid = str(slot.get("stockId", "unknown"))
                        logger.warning("Duplicate StockId at insert (savepoint rolled back): %s", sid)
                        runtime_dup_warnings.append({
                            "stockId":     sid,
                            "productCode": var["rec"].get("productCode"),
                            "variantCode": var["rec"].get("variantCode"),
                            "message":     f"Stock ID '{sid}' already exists — skipped.",
                        })
                        skipped_duplicates += 1
                        total_skipped      += 1

        db.commit()     # commit everything that succeeded

    except Exception as exc:
        db.rollback()
        logger.exception("Product bulk import failed — full rollback: %s", exc)
        raise HTTPException(status_code=500,
            detail=f"Import aborted and fully rolled back due to a DB error: {exc}")

    duplicate_stock_warnings.extend(runtime_dup_warnings)

    parts = [
        f"Successfully imported {inserted_products} product(s), "
        f"{inserted_variants} variant(s), {inserted_details} size detail(s)."
    ]
    if skipped_validation:
        parts.append(f"{skipped_validation} row(s) skipped — validation errors.")
    if skipped_duplicates:
        parts.append(f"{skipped_duplicates} row(s) skipped — duplicate Stock IDs.")

    return {
        "message":                " ".join(parts),
        "insertedProducts":       inserted_products,
        "insertedVariants":       inserted_variants,
        "insertedDetails":        inserted_details,
        "skippedRows":            total_skipped,
        "skippedValidation":      skipped_validation,
        "skippedDuplicates":      skipped_duplicates,
        "hsnWarnings":            hsn_warnings,
        "duplicateStockWarnings": duplicate_stock_warnings,
    }