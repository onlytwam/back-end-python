import io
import json
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import Optional

from database import get_db
from app.auth.dependencies import get_current_user, Roles
from . import schemas, repository


router = APIRouter(prefix="/api/Coupons", tags=["Coupons"])


# ─────────────────────────────────────────────
# Helper: parse Filters query param
# ─────────────────────────────────────────────
def _pf(Filters: Optional[str]):
    if not Filters:
        return None
    try:
        parsed = json.loads(Filters)
        return parsed if isinstance(parsed, list) else [parsed]
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Filters format.")


# ══════════════════════════════════════════════
# DIGITAL COUPONS
# ══════════════════════════════════════════════

# ─────────────────────────────────────────────
# GET /  — Admin: all coupons
# ─────────────────────────────────────────────
@router.get("/", response_model=schemas.CouponListResponse)
def get_coupons(
    Filters:         Optional[str]  = Query(None, alias="Filters"),
    Order_Ascending: Optional[bool] = Query(None, alias="Order.Ascending"),
    Order_Property:  Optional[str]  = Query(None, alias="Order.Property"),
    Page_Index:      Optional[int]  = Query(None, alias="Page.Index", ge=1),
    Page_Size:       Optional[int]  = Query(None, alias="Page.Size",  ge=1),
    db: Session = Depends(get_db),
):
    result = repository.get_all_coupons(
        db, _pf(Filters), Order_Ascending, Order_Property, Page_Index, Page_Size
    )
    return {
        "count": result["count"],
        "list": [
            {
                "couponId":       c.couponId,
                "couponCode":     c.couponCode,
                "couponName":     c.couponName,
                "description":    c.description,
                "discount":       c.discount,
                "startDate":      c.startDate,
                "endDate":        c.endDate,
                "state":          c.state,
                "isActive":       c.isActive,
                "isCommon":       c.isCommon,
                "toUserProfileId":c.toUserProfileId,
                "couponType":     c.couponType,
                "productId":      c.productId,
            }
            for c in result["list"]
        ],
        "parameters": None,
    }


# ─────────────────────────────────────────────
# GET /UserList  — User: available coupons (excludes used)
# ─────────────────────────────────────────────
@router.get("/UserList", response_model=schemas.CouponUserListResponse)
def get_user_coupons(
    Filters:         Optional[str]  = Query(None, alias="Filters"),
    Order_Ascending: Optional[bool] = Query(None, alias="Order.Ascending"),
    Order_Property:  Optional[str]  = Query(None, alias="Order.Property"),
    Page_Index:      Optional[int]  = Query(None, alias="Page.Index", ge=1),
    Page_Size:       Optional[int]  = Query(None, alias="Page.Size",  ge=1),
    db: Session = Depends(get_db),
):
    parsed_filters = _pf(Filters)
    user_id = next(
        (f.get("value") for f in (parsed_filters or []) if f.get("property") == "UserProfileId"),
        None,
    )

    result = repository.get_user_coupons(
        db, user_id, parsed_filters, Order_Ascending, Order_Property, Page_Index, Page_Size
    )

    return {
        "count": result["count"],
        "list": [
            {
                "couponId":       c.couponId,
                "couponCode":     c.couponCode,
                "couponName":     c.couponName,
                "description":    c.description,
                "discount":       c.discount,
                "startDate":      c.startDate,
                "endDate":        c.endDate,
                "state":          c.state,
                "isActive":       c.isActive,
                "isCommon":       c.isCommon,
                "toUserProfileId":c.toUserProfileId,
                "couponType":     c.couponType,
                "productId":      c.productId,
                "isExpire":       getattr(c, "isExpire", False),
            }
            for c in result["list"]
        ],
        "parameters": None,
    }


# ─────────────────────────────────────────────
# GET /{coupon_id}
# ─────────────────────────────────────────────
@router.get("/{coupon_id}", response_model=schemas.CouponResponse)
def get_coupon(coupon_id: int, db: Session = Depends(get_db)):
    coupon = repository.get_coupon_by_id(db, coupon_id)
    if not coupon:
        raise HTTPException(status_code=404, detail="Coupon not found")
    return coupon


# ─────────────────────────────────────────────
# POST /  — Create coupon (admin)
# ─────────────────────────────────────────────
@router.post("/", response_model=schemas.CouponResponse, status_code=201,
             dependencies=[Depends(get_current_user)])
def create_coupon(coupon: schemas.CouponCreate, db: Session = Depends(get_db)):
    return repository.create_coupon(db, coupon.model_dump())


# ─────────────────────────────────────────────
# PUT /{coupon_id}  — Update coupon (admin)
# ─────────────────────────────────────────────
@router.put("/{coupon_id}", response_model=schemas.CouponResponse,
            dependencies=[Depends(get_current_user)])
def update_coupon(coupon_id: int, coupon: schemas.CouponUpdate, db: Session = Depends(get_db)):
    updated = repository.update_coupon(db, coupon_id, coupon.model_dump(exclude_unset=True))
    if not updated:
        raise HTTPException(status_code=404, detail="Coupon not found")
    return updated


# ─────────────────────────────────────────────
# DELETE /{coupon_id}  — Soft delete (admin)
# ─────────────────────────────────────────────
@router.delete("/{coupon_id}", status_code=204, dependencies=[Depends(get_current_user)])
def delete_coupon(coupon_id: int, db: Session = Depends(get_db)):
    if not repository.delete_coupon(db, coupon_id):
        raise HTTPException(status_code=404, detail="Coupon not found")


# ─────────────────────────────────────────────
# POST /MarkUsed  — Mark digital coupon as used after order placed
# ─────────────────────────────────────────────
@router.post("/MarkUsed", status_code=200, dependencies=[Depends(get_current_user)])
def mark_digital_coupon_used(
    payload: schemas.CouponMarkUsedRequest,
    db: Session = Depends(get_db),
):
    """
    Called by the frontend immediately after a successful order is placed.
    Idempotent – safe to call multiple times.
    """
    repository.mark_digital_coupon_used(
        db,
        coupon_id       = payload.couponId,
        user_profile_id = payload.userProfileId,
        order_id        = payload.orderId,
    )
    return {"success": True}


# ══════════════════════════════════════════════
# PHYSICAL COUPONS
# ══════════════════════════════════════════════

# ─────────────────────────────────────────────
# POST /Physical/Validate  — Validate a physical coupon code
# ─────────────────────────────────────────────
@router.post("/Physical/Validate", response_model=schemas.PhysicalCouponValidateResponse)
def validate_physical_coupon(
    payload: schemas.PhysicalCouponValidateRequest,
    db: Session = Depends(get_db),
):
    """
    Returns coupon details if valid and unused.
    Returns valid=False if code not found.
    Returns 409 if code already used.
    """
    coupon = repository.validate_physical_coupon(db, payload.couponCode)
    if not coupon:
        return schemas.PhysicalCouponValidateResponse(
            valid      = False,
            couponCode = payload.couponCode.upper(),
            message    = "Invalid coupon code",
        )
    return schemas.PhysicalCouponValidateResponse(
        valid       = True,
        couponCode  = coupon.couponCode,
        discount    = coupon.discount,
        description = coupon.description,
        message     = "Coupon is valid",
    )


# ─────────────────────────────────────────────
# POST /Physical/MarkUsed  — Redeem physical coupon after order placed
# ─────────────────────────────────────────────
@router.post("/Physical/MarkUsed", status_code=200, dependencies=[Depends(get_current_user)])
def mark_physical_coupon_used(
    payload: schemas.PhysicalCouponMarkUsedRequest,
    db: Session = Depends(get_db),
):
    repository.mark_physical_coupon_used(
        db,
        coupon_code    = payload.couponCode,
        user_profile_id= payload.userProfileId,
        order_id       = payload.orderId,
    )
    return {"success": True}


# ─────────────────────────────────────────────
# GET /Physical  — Admin: list all physical coupons
# ─────────────────────────────────────────────
@router.get("/Physical", response_model=schemas.PhysicalCouponListResponse,
            dependencies=[Depends(get_current_user)])
def list_physical_coupons(
    Page_Index: Optional[int] = Query(None, alias="Page.Index", ge=1),
    Page_Size:  Optional[int] = Query(None, alias="Page.Size",  ge=1),
    db: Session = Depends(get_db),
):
    result = repository.get_all_physical_coupons(db, Page_Index, Page_Size)
    return {"count": result["count"], "list": result["list"]}


# ─────────────────────────────────────────────
# GET /Physical/Template  — Admin: download Excel template
# ─────────────────────────────────────────────
@router.get("/Physical/Template", dependencies=[Depends(get_current_user)])
def download_physical_coupon_template():
    """
    Returns a styled .xlsx template with the correct column headers and one
    dummy row so admins know exactly what to fill in before uploading.

    Columns
    -------
    CouponCode  — required, unique coupon code printed on the physical card
    Discount    — required, integer percentage (e.g. 10 means 10 % off)
    Description — optional, short human-readable label shown at checkout

    The sample row (row 2) uses italic blush styling so admins can clearly
    identify and replace it.  No extra rows are included — the uploader
    reads every row from row 2 onward, so any stray text below would be
    treated as coupon data.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Physical Coupons"

    # ── Style helpers ──────────────────────────────────────────────────────
    HEADER_FILL = PatternFill("solid", fgColor="6B0F2A")   # brand burgundy
    HEADER_FONT = Font(name="Calibri", bold=True,   color="FFFFFF", size=11)
    SAMPLE_FILL = PatternFill("solid", fgColor="FFF5F7")   # light blush
    SAMPLE_FONT = Font(name="Calibri", italic=True, color="555555", size=10)
    CENTER      = Alignment(horizontal="center", vertical="center")
    LEFT        = Alignment(horizontal="left",   vertical="center")
    thin        = Side(border_style="thin", color="CCCCCC")
    BORDER      = Border(left=thin, right=thin, top=thin, bottom=thin)

    # ── Column definitions: (header, width, data_alignment, sample_value) ─
    COLUMNS = [
        ("CouponCode",  24, CENTER, "SUMMER10OFF"),
        ("Discount",    14, CENTER, 10),
        ("Description", 42, LEFT,   "10% off your purchase"),
    ]

    # ── Row 1 : headers ────────────────────────────────────────────────────
    ws.row_dimensions[1].height = 24
    for col_idx, (label, width, _align, _sample) in enumerate(COLUMNS, start=1):
        cell           = ws.cell(row=1, column=col_idx, value=label)
        cell.font      = HEADER_FONT
        cell.fill      = HEADER_FILL
        cell.alignment = CENTER
        cell.border    = BORDER
        ws.column_dimensions[cell.column_letter].width = width

    # ── Row 2 : one clearly-styled sample row (replace before uploading) ───
    ws.row_dimensions[2].height = 20
    for col_idx, (_label, _w, align, sample) in enumerate(COLUMNS, start=1):
        cell           = ws.cell(row=2, column=col_idx, value=sample)
        cell.font      = SAMPLE_FONT
        cell.fill      = SAMPLE_FILL
        cell.alignment = align
        cell.border    = BORDER

    # ── Freeze the header row so it stays visible while scrolling ──────────
    ws.freeze_panes = "A2"

    # ── Sheet-level instructions in a separate tab ─────────────────────────
    info = wb.create_sheet("Instructions")
    info.column_dimensions["A"].width = 70
    INFO_HEADER_FONT = Font(name="Calibri", bold=True, color="6B0F2A", size=12)
    INFO_BODY_FONT   = Font(name="Calibri", size=10)
    rows = [
        ("How to upload Physical Coupons", INFO_HEADER_FONT),
        ("", None),
        ("1.  Go to the 'Physical Coupons' sheet (the first tab).", INFO_BODY_FONT),
        ("2.  Delete row 2 (the sample row in light pink) and add your real coupon data.", INFO_BODY_FONT),
        ("3.  Each row = one coupon code.  Do NOT leave blank rows between data rows.", INFO_BODY_FONT),
        ("4.  Save the file and upload it via the Admin → Coupons → Upload button.", INFO_BODY_FONT),
        ("", None),
        ("Column guide:", Font(name="Calibri", bold=True, size=10)),
        ("  CouponCode  (required) — Unique code printed on the physical card.  No spaces.", INFO_BODY_FONT),
        ("  Discount    (required) — Integer percentage, e.g. 10 means 10 % off.", INFO_BODY_FONT),
        ("  Description (optional) — Short label shown to the customer at checkout.", INFO_BODY_FONT),
        ("", None),
        ("Rules:", Font(name="Calibri", bold=True, size=10)),
        ("  • Each code can only be used once by any customer.", INFO_BODY_FONT),
        ("  • Duplicate codes are automatically skipped on upload.", INFO_BODY_FONT),
        ("  • Codes are matched case-insensitively.", INFO_BODY_FONT),
    ]
    for row_num, (text, font) in enumerate(rows, start=1):
        cell = info.cell(row=row_num, column=1, value=text)
        if font:
            cell.font = font
        cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
        info.row_dimensions[row_num].height = 18

    # ── Stream back as a file download ─────────────────────────────────────
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="physical_coupons_template.xlsx"'},
    )


# ─────────────────────────────────────────────
# POST /Physical/Upload  — Admin: bulk upload via Excel
# ─────────────────────────────────────────────
@router.post("/Physical/Upload", status_code=201, dependencies=[Depends(get_current_user)])
async def upload_physical_coupons(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """
    Accepts an .xlsx file.  Required column: CouponCode.
    Optional columns: Discount (integer %), Description.
    Duplicate codes are silently skipped.
    """
    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(400, "Only Excel files (.xlsx / .xls) are accepted")

    contents = await file.read()
    try:
        wb = openpyxl.load_workbook(io.BytesIO(contents), read_only=True, data_only=True)
        ws = wb.active
    except Exception:
        raise HTTPException(400, "Could not parse the Excel file")

    headers = [str(cell.value or "").strip().lower() for cell in next(ws.iter_rows(min_row=1, max_row=1))]

    def col(row, name: str):
        try:
            idx = headers.index(name.lower())
            v   = row[idx].value
            return v
        except (ValueError, IndexError):
            return None

    if "couponcode" not in headers:
        raise HTTPException(400, "Excel must have a 'CouponCode' column header")

    codes = []
    for row in ws.iter_rows(min_row=2):
        code = col(row, "couponcode")
        if not code:
            continue
        discount_val = col(row, "discount")
        codes.append({
            "couponCode":  str(code).strip(),
            "discount":    int(discount_val) if discount_val is not None else None,
            "description": str(col(row, "description") or "").strip() or None,
        })

    if not codes:
        raise HTTPException(400, "No coupon codes found in the file")

    inserted, skipped = repository.bulk_create_physical_coupons(db, codes)

    return {
        "message":  f"Upload complete: {inserted} inserted, {skipped} skipped (duplicates or blank)",
        "inserted": inserted,
        "skipped":  skipped,
    }