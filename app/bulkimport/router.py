"""
app/bulkimport/router.py
─────────────────────────
Bulk import endpoint — accepts an Excel (.xlsx) file and creates records
in bulk for: Products, Category, brands, Size, Coupons.
"""

import io
from datetime import datetime, timezone,timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import text


try:
    import openpyxl
    from openpyxl import Workbook
    OPENPYXL_AVAILABLE = True
except ImportError:
    OPENPYXL_AVAILABLE = False

from database import get_db
from app.auth.dependencies import get_current_user, require_roles, Roles, CurrentUser

router = APIRouter(
    prefix="/api/BulkImport",
    tags=["Bulk Import"],
    dependencies=[Depends(get_current_user)],
)

# ── Entity config ──────────────────────────────────────────────────────────────
# key must match what the frontend sends (case-sensitive)

ENTITY_CONFIG = {
    "Products": {
        "columns": [
            ("productCode",     "Product Code*",    "str", True),
            ("name",            "Product Name*",    "str", True),
            ("description",     "Description*",     "str", True),
            ("categoryId",      "Category ID*",     "int", True),
            ("brandId",         "Brand ID*",        "int", True),
            ("tag",             "Tags",             "str", False),
            ("childCategoryId", "Child Category ID","int", False),
        ],
        "required": ["productCode", "name", "description", "categoryId", "brandId"],
        "notes": "Get Category ID from Category page · Get Brand ID from Brands page",
    },
    "Category": {
        "columns": [
            ("Name",             "Name*",              "str", True),
            ("Description",      "Description",        "str", False),
            ("ParentCategoryId", "Parent Category ID", "int", False),
        ],
        "required": ["Name"],
        "notes": "Leave Parent Category ID blank for top-level categories",
    },
    "brands": {
        "columns": [
            ("brandName",        "Brand Name*",  "str", True),
            ("brandDescription", "Description",  "str", False),
        ],
        "required": ["brandName"],
        "notes": "Brand images must be uploaded separately via the Brands page",
    },
    "Size": {
        "columns": [
            ("sizeLabel",   "Size Label*", "str", True),
            ("sizeCode",    "Size Code*",  "str", True),
            ("description", "Description","str", False),
            ("dimensions",  "Dimensions", "str", False),
            ("orderNo",     "Order No",   "int", False),
        ],
        "required": ["sizeLabel", "sizeCode"],
        "notes": "Order No controls the display order (1 = first)",
    },
    "Coupons": {
        "columns": [
            ("CouponCode", "Coupon Code*", "str", True),
            ("CouponName", "Coupon Name*", "str", True),
            ("Description", "Description", "str", False),
            ("Discount", "Discount*", "float", True),
            ("ExpiryDate", "Expiry Date", "str", False),
        ],
        "required": ["CouponCode", "CouponName", "Discount"],
        "notes": "Expiry Date format: YYYY-MM-DD",
    },
}

# ── Helpers ────────────────────────────────────────────────────────────────────

def _check_openpyxl():
    if not OPENPYXL_AVAILABLE:
        raise HTTPException(
            status_code=500,
            detail="openpyxl is not installed. Run: pip install openpyxl",
        )


def _cast(value, dtype: str):
    if value is None or str(value).strip() == "":
        return None
    try:
        if dtype == "int":
            return int(float(str(value).strip()))
        elif dtype == "float":
            return float(str(value).strip())
        else:
            return str(value).strip()
    except (ValueError, TypeError):
        raise ValueError(f"Cannot cast '{value}' to {dtype}")


def _parse_excel(file_bytes: bytes, entity: str) -> list[dict]:
    _check_openpyxl()
    cfg = ENTITY_CONFIG[entity]
    col_defs = cfg["columns"]

    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot read Excel file: {e}")

    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise HTTPException(status_code=400, detail="The Excel file is empty.")

    # Map header → column index (case-insensitive, strip asterisks)
    header_row = [str(h).strip().rstrip("*").strip() if h else "" for h in rows[0]]
    field_map: dict[str, int] = {}
    for col_idx, header in enumerate(header_row):
        for field, col_label, dtype, req in col_defs:
            clean_label = col_label.rstrip("*").strip()
            if header.lower() == clean_label.lower() or header.lower() == field.lower():
                field_map[field] = col_idx

    for field, label, dtype, req in col_defs:
        if req and field not in field_map:
            raise HTTPException(
                status_code=400,
                detail=f"Required column '{label}' not found. Download the template first.",
            )

    dtype_map = {field: dtype for field, _, dtype, _ in col_defs}

    result = []
    for row_num, row in enumerate(rows[1:], start=2):
        if all(cell is None or str(cell).strip() == "" for cell in row):
            continue

        record: dict = {}
        row_errors = []

        for field, label, dtype, required in col_defs:
            if field not in field_map:
                record[field] = None
                continue
            raw = row[field_map[field]] if field_map[field] < len(row) else None
            try:
                val = _cast(raw, dtype_map[field])
            except ValueError:
                row_errors.append(f"Column '{label}': invalid value '{raw}'")
                val = None

            if required and val is None:
                row_errors.append(f"Column '{label}' is required")

            record[field] = val

        record["_row"] = row_num
        record["_errors"] = row_errors
        result.append(record)

    return result


# ── GET /entities ──────────────────────────────────────────────────────────────

@router.get("/entities")
def list_entities():
    return {
        "entities": [
            {
                "key": k,
                "label": k.replace("_", " ").title(),
                "columns": [c[1] for c in v["columns"]],
                "required": v["required"],
                "notes": v["notes"],
            }
            for k, v in ENTITY_CONFIG.items()
        ]
    }


# ── GET /template/{entity} ─────────────────────────────────────────────────────

@router.get("/template/{entity}")
def download_template(entity: str):
    _check_openpyxl()
    if entity not in ENTITY_CONFIG:
        raise HTTPException(status_code=404, detail=f"Entity '{entity}' not supported.")

    cfg = ENTITY_CONFIG[entity]
    col_defs = cfg["columns"]

    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = entity.title()

    header_font  = Font(bold=True, color="FFFFFF", size=11)
    header_fill  = PatternFill("solid", fgColor="6B0F2A")
    req_fill     = PatternFill("solid", fgColor="F5E6EA")
    center_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin_border  = Border(
        left=Side(style="thin", color="DDDDDD"),
        right=Side(style="thin", color="DDDDDD"),
        top=Side(style="thin", color="DDDDDD"),
        bottom=Side(style="thin", color="DDDDDD"),
    )

    for col_idx, (field, label, dtype, required) in enumerate(col_defs, start=1):
        cell = ws.cell(row=1, column=col_idx, value=label)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center_align
        cell.border = thin_border
        ws.column_dimensions[get_column_letter(col_idx)].width = max(20, len(label) + 4)

    SAMPLE_ROW = {
        "productCode":    "PRD-001",
        "name":           "Sample Product",
        "description":    "A brief description of the product",
        "categoryId":     1,
        "brandId":        1,
        "tag":            "summer,casual",
        "childCategoryId":"",
        "brandName":      "Sample Brand",
        "brandDescription":"Brand description here",
        "name_brand":     "Sample Brand",
        "name_cat":       "Category Name",
        "description_cat":"Category description",
        "parentCategoryId":"",
        "sizeLabel":      "S",
        "sizeCode":       "S",
        "dimensions":     "Chest: 34-36in",
        "orderNo":        1,
        "couponCode":     "SAVE10",
        "discountType":   "Percent",
        "discountValue":  10,
        "minOrderAmount": 500,
        "maxUses":        100,
        "expiryDate":     "2025-12-31",
    }

    for col_idx, (field, label, dtype, required) in enumerate(col_defs, start=1):
        cell = ws.cell(row=2, column=col_idx, value=SAMPLE_ROW.get(field, ""))
        if required:
            cell.fill = req_fill
        cell.border = thin_border

    ws.cell(row=4, column=1, value="Notes:").font = Font(bold=True, size=10)
    ws.cell(row=5, column=1, value=cfg["notes"]).font = Font(italic=True, color="666666", size=9)
    ws.cell(row=6, column=1, value="Required fields are marked with * in the header").font = Font(italic=True, color="666666", size=9)
    ws.cell(row=7, column=1, value="Row 2 is a sample — replace with your data and delete it").font = Font(italic=True, color="666666", size=9)

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


# ── POST /preview/{entity} ────────────────────────────────────────────────────

@router.post("/preview/{entity}")
async def preview_import(entity: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    if entity not in ENTITY_CONFIG:
        raise HTTPException(status_code=404, detail=f"Entity '{entity}' not supported.")

    content = await file.read()
    rows = _parse_excel(content, entity)

    return {
        "entity":      entity,
        "totalRows":   len(rows),
        "validRows":   sum(1 for r in rows if not r["_errors"]),
        "invalidRows": sum(1 for r in rows if r["_errors"]),
        "columns":     [c[1] for c in ENTITY_CONFIG[entity]["columns"]],
        "rows":        rows[:200],
        "canImport":   any(not r["_errors"] for r in rows),
    }


# ── POST /import/{entity} ─────────────────────────────────────────────────────

@router.post("/import/{entity}")
async def execute_import(
    entity: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    if entity not in ENTITY_CONFIG:
        raise HTTPException(status_code=404, detail=f"Entity '{entity}' not supported.")

    if current_user.role_id not in {Roles.SUPER_ADMIN, Roles.PRODUCT_MANAGER, Roles.INVENTORY_MANAGER}:
        raise HTTPException(status_code=403, detail="Insufficient permissions.")

    content = await file.read()
    rows = _parse_excel(content, entity)

    inserted = 0
    skipped = 0
    row_errors = []
    now = datetime.now(timezone.utc)

    for row in rows:
        if row["_errors"]:
            skipped += 1
            row_errors.append({"row": row["_row"], "errors": row["_errors"]})
            continue
        try:
            _insert_row(db, entity, row, current_user.user_id, now)
            inserted += 1
        except Exception as e:
            db.rollback()
            skipped += 1
            row_errors.append({"row": row["_row"], "errors": [str(e)]})

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Commit failed: {e}")

    return {
        "entity":   entity,
        "inserted": inserted,
        "skipped":  skipped,
        "errors":   row_errors[:50],
        "message":  f"Successfully imported {inserted} {entity}. {skipped} rows skipped.",
    }


# ── Insert logic — column names match actual DB tables exactly ─────────────────

def _insert_row(db: Session, entity: str, row: dict, user_id: str, now: datetime):

    if entity == "Products":
        # twam."Products" — PascalCase columns
        db.execute(text("""
            INSERT INTO twam."Products"
              ("ProductCode", "Name", "Description",
               "CategoryId", "ChildCategoryId", "BrandId", "Tag",
               "State", "UserProfileId", "CreatedBy", "ModifiedBy",
               "CreatedDate", "ModifiedDate", "DeletedInd")
            VALUES
              (:productCode, :name, :description,
               :categoryId, :childCategoryId, :brandId, :tag,
               'Created', :userId, :userId, :userId,
               :now, :now, false)
        """), {**row, "userId": user_id, "now": now})

    elif entity == "Category":
        db.execute(text("""
            INSERT INTO twam."Category"
            ("Name", "Description", "ParentCategoryId",
            "IsActive", "CreatedBy", "ModifiedBy",
            "CreatedDate", "ModifiedDate", "DeletedInd")
            VALUES
            (:Name, :Description, :ParentCategoryId,
            true, :userId, :userId,
            :now, :now, false)
        """), {**row, "userId": user_id, "now": now})

    elif entity == "brands":
        db.execute(text("""
            INSERT INTO mdm."brands"
              ("brandName", "brandDescription", "userProfileId",
               "state", "isActive", "createdDate", "modifiedDate", "deletedInd")
            VALUES
              (:brandName, :brandDescription, :userId,
               'Active', true, :now, :now, false)
        """), {**row, "userId": user_id, "now": now})

    elif entity == "Size":
        db.execute(text("""
            INSERT INTO mdm."Size"
            ("SizeLabel", "SizeCode", "Description", "Dimensions", "OrderNo",
            "State", "IsActive", "UserProfileId",
            "CreatedDate", "ModifiedDate", "DeletedInd")
            VALUES
            (:sizeLabel, :sizeCode, :description, :dimensions, :orderNo,
            'Active', true, :userId,
            :now, :now, false)
        """), {**row, "userId": user_id, "now": now})

    elif entity == "Coupons":
        from datetime import datetime, timedelta, timezone

        start = None
        end = None

        # START DATE
        start_val = row.get("StartDate")
        if start_val:
            try:
                if isinstance(start_val, (int, float)):
                    start = datetime(1899, 12, 30) + timedelta(days=int(start_val))
                else:
                    start = datetime.strptime(start_val, "%Y-%m-%d")

                start = start.replace(tzinfo=timezone.utc)
            except:
                start = None

        # END DATE (ExpiryDate)
        end_val = row.get("ExpiryDate")
        if end_val:
            try:
                if isinstance(end_val, (int, float)):
                    end = datetime(1899, 12, 30) + timedelta(days=int(end_val))
                else:
                    end = datetime.strptime(end_val, "%Y-%m-%d")

                end = end.replace(tzinfo=timezone.utc)
            except:
                end = None

        db.execute(text("""
                INSERT INTO twam."Coupons"
                ("CouponCode", "CouponName", "Description", "Discount",
                "StartDate", "EndDate",
                "IsActive", "UserProfileId",
                "CreatedDate", "ModifiedDate", "DeletedInd")
                VALUES
                (:CouponCode, :CouponCode, :Description, :Discount,
                :StartDate, :EndDate,
                true, :userId,
                :now, :now, false)
            """), {**row,
                "StartDate": start,
                "EndDate": end,
                "userId": user_id,
                "now": now
            })
    else:
        raise ValueError(f"No insert handler for entity '{entity}'")