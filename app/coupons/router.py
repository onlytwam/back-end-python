"""
app/coupons/router.py
─────────────────────
All coupon endpoints: digital, physical, user-specific bulk assignment.

NEW endpoints added (User-Specific Bulk Coupons):
  GET  /api/Coupons/UserSegments                 → list all segment types
  POST /api/Coupons/UserSegments/Preview         → preview users for a segment (JSON)
  POST /api/Coupons/UserSegments/GenerateExcel   → download Excel of user IDs for a segment
  GET  /api/Coupons/UserSpecific/Template        → download Excel template for manual upload
  POST /api/Coupons/UserSpecific/Upload          → upload Excel with user IDs → assign coupons
  POST /api/Coupons/UserSpecific/AssignFromSegment → assign coupons to a segment directly
  GET  /api/Coupons/UserSpecific/Batches         → list all batch assignments (admin history)
  PUT  /api/Coupons/UserSpecific/Batches/{id}/Deactivate → cancel a batch
"""

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


# ─── Style constants (shared across Excel generators) ────────────────────────
BRAND_COLOR  = "6B0F2A"   # burgundy header
ACCENT_COLOR = "F5E6EA"   # light blush rows
WHITE        = "FFFFFF"
GREY_TEXT    = "555555"

def _header_style():
    thin = Side(border_style="thin", color="CCCCCC")
    return (
        Font(name="Calibri", bold=True, color=WHITE, size=11),
        PatternFill("solid", fgColor=BRAND_COLOR),
        Alignment(horizontal="center", vertical="center"),
        Border(left=thin, right=thin, top=thin, bottom=thin),
    )

def _sample_style():
    thin = Side(border_style="thin", color="DDDDDD")
    return (
        Font(name="Calibri", italic=True, color=GREY_TEXT, size=10),
        PatternFill("solid", fgColor=ACCENT_COLOR),
        Border(left=thin, right=thin, top=thin, bottom=thin),
    )

def _data_style():
    thin = Side(border_style="thin", color="DDDDDD")
    return (
        Font(name="Calibri", size=10),
        PatternFill("solid", fgColor=WHITE),
        Border(left=thin, right=thin, top=thin, bottom=thin),
    )

def _apply(cell, font, fill, alignment=None, border=None):
    cell.font = font
    cell.fill = fill
    if alignment: cell.alignment = alignment
    if border:    cell.border    = border

def _excel_response(wb: openpyxl.Workbook, filename: str) -> StreamingResponse:
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ─── Helper: parse Filters query param ───────────────────────────────────────
def _pf(Filters: Optional[str]):
    if not Filters:
        return None
    try:
        parsed = json.loads(Filters)
        return parsed if isinstance(parsed, list) else [parsed]
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Filters format.")


# ══════════════════════════════════════════════════════════════════════════════
# DIGITAL COUPONS  (existing endpoints — unchanged)
# ══════════════════════════════════════════════════════════════════════════════

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
                "couponId": c.couponId, "couponCode": c.couponCode,
                "couponName": c.couponName, "description": c.description,
                "discount": c.discount, "startDate": c.startDate,
                "endDate": c.endDate, "state": c.state, "isActive": c.isActive,
                "isCommon": c.isCommon, "toUserProfileId": c.toUserProfileId,
                "couponType": c.couponType, "productId": c.productId,
            }
            for c in result["list"]
        ],
        "parameters": None,
    }


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
                "couponId": c.couponId, "couponCode": c.couponCode,
                "couponName": c.couponName, "description": c.description,
                "discount": c.discount, "startDate": c.startDate,
                "endDate": c.endDate, "state": c.state, "isActive": c.isActive,
                "isCommon": c.isCommon, "toUserProfileId": c.toUserProfileId,
                "couponType": c.couponType, "productId": c.productId,
                "isExpire": getattr(c, "isExpire", False),
            }
            for c in result["list"]
        ],
        "parameters": None,
    }


@router.get("/{coupon_id}", response_model=schemas.CouponResponse)
def get_coupon(coupon_id: int, db: Session = Depends(get_db)):
    coupon = repository.get_coupon_by_id(db, coupon_id)
    if not coupon:
        raise HTTPException(status_code=404, detail="Coupon not found")
    return coupon


@router.post("/", response_model=schemas.CouponResponse, status_code=201,
             dependencies=[Depends(get_current_user)])
def create_coupon(coupon: schemas.CouponCreate, db: Session = Depends(get_db)):
    return repository.create_coupon(db, coupon.model_dump())


@router.put("/{coupon_id}", response_model=schemas.CouponResponse,
            dependencies=[Depends(get_current_user)])
def update_coupon(coupon_id: int, coupon: schemas.CouponUpdate, db: Session = Depends(get_db)):
    updated = repository.update_coupon(db, coupon_id, coupon.model_dump(exclude_unset=True))
    if not updated:
        raise HTTPException(status_code=404, detail="Coupon not found")
    return updated


@router.delete("/{coupon_id}", status_code=204, dependencies=[Depends(get_current_user)])
def delete_coupon(coupon_id: int, db: Session = Depends(get_db)):
    if not repository.delete_coupon(db, coupon_id):
        raise HTTPException(status_code=404, detail="Coupon not found")


@router.post("/MarkUsed", status_code=200, dependencies=[Depends(get_current_user)])
def mark_digital_coupon_used(payload: schemas.CouponMarkUsedRequest, db: Session = Depends(get_db)):
    repository.mark_digital_coupon_used(db, payload.couponId, payload.userProfileId, payload.orderId)
    return {"success": True}


# ══════════════════════════════════════════════════════════════════════════════
# PHYSICAL COUPONS  (existing endpoints — unchanged)
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/Physical/Validate", response_model=schemas.PhysicalCouponValidateResponse)
def validate_physical_coupon(payload: schemas.PhysicalCouponValidateRequest, db: Session = Depends(get_db)):
    coupon = repository.validate_physical_coupon(db, payload.couponCode)
    if not coupon:
        return schemas.PhysicalCouponValidateResponse(
            valid=False, couponCode=payload.couponCode.upper(), message="Invalid coupon code"
        )
    return schemas.PhysicalCouponValidateResponse(
        valid=True, couponCode=coupon.couponCode,
        discount=coupon.discount, description=coupon.description, message="Coupon is valid",
    )


@router.post("/Physical/MarkUsed", status_code=200, dependencies=[Depends(get_current_user)])
def mark_physical_coupon_used(payload: schemas.PhysicalCouponMarkUsedRequest, db: Session = Depends(get_db)):
    repository.mark_physical_coupon_used(db, payload.couponCode, payload.userProfileId, payload.orderId)
    return {"success": True}


@router.get("/Physical", response_model=schemas.PhysicalCouponListResponse,
            dependencies=[Depends(get_current_user)])
def list_physical_coupons(
    Page_Index: Optional[int] = Query(None, alias="Page.Index", ge=1),
    Page_Size:  Optional[int] = Query(None, alias="Page.Size",  ge=1),
    db: Session = Depends(get_db),
):
    result = repository.get_all_physical_coupons(db, Page_Index, Page_Size)
    return {"count": result["count"], "list": result["list"]}


@router.get("/Physical/Template", dependencies=[Depends(get_current_user)])
def download_physical_coupon_template():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Physical Coupons"
    hf, hfill, ha, hb = _header_style()
    sf, sfill, sb     = _sample_style()
    CENTER = Alignment(horizontal="center", vertical="center")
    LEFT   = Alignment(horizontal="left",   vertical="center")

    COLUMNS = [
        ("CouponCode",  24, CENTER, "SUMMER10OFF"),
        ("Discount",    14, CENTER, 10),
        ("Description", 42, LEFT,   "10% off your purchase"),
    ]
    ws.row_dimensions[1].height = 24
    for ci, (label, width, _, _s) in enumerate(COLUMNS, 1):
        cell = ws.cell(row=1, column=ci, value=label)
        _apply(cell, hf, hfill, ha, hb)
        ws.column_dimensions[cell.column_letter].width = width
    ws.row_dimensions[2].height = 20
    for ci, (_, _, align, sample) in enumerate(COLUMNS, 1):
        cell = ws.cell(row=2, column=ci, value=sample)
        _apply(cell, sf, sfill, align, sb)
    ws.freeze_panes = "A2"
    return _excel_response(wb, "physical_coupons_template.xlsx")


@router.post("/Physical/Upload", status_code=201, dependencies=[Depends(get_current_user)])
async def upload_physical_coupons(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(400, "Only Excel files (.xlsx / .xls) are accepted")
    contents = await file.read()
    try:
        wb = openpyxl.load_workbook(io.BytesIO(contents), read_only=True, data_only=True)
        ws = wb.active
    except Exception:
        raise HTTPException(400, "Could not parse the Excel file")
    headers = [str(cell.value or "").strip().lower() for cell in next(ws.iter_rows(min_row=1, max_row=1))]
    def col(row, name):
        try:
            return row[headers.index(name.lower())].value
        except (ValueError, IndexError):
            return None
    if "couponcode" not in headers:
        raise HTTPException(400, "Excel must have a 'CouponCode' column header")
    codes = []
    for row in ws.iter_rows(min_row=2):
        code = col(row, "couponcode")
        if not code:
            continue
        dv = col(row, "discount")
        codes.append({
            "couponCode":  str(code).strip(),
            "discount":    int(dv) if dv is not None else None,
            "description": str(col(row, "description") or "").strip() or None,
        })
    if not codes:
        raise HTTPException(400, "No coupon codes found in the file")
    inserted, skipped = repository.bulk_create_physical_coupons(db, codes)
    return {
        "message":  f"Upload complete: {inserted} inserted, {skipped} skipped",
        "inserted": inserted,
        "skipped":  skipped,
    }


# ══════════════════════════════════════════════════════════════════════════════
# USER SEGMENTS  (NEW)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/UserSegments", response_model=schemas.SegmentListResponse,
            dependencies=[Depends(get_current_user)])
def list_user_segments():
    """
    Returns all available customer segment types.
    Admin uses this to populate the segment picker in the UI.
    """
    return {"segments": repository.SEGMENT_DEFINITIONS}


@router.post("/UserSegments/Preview", response_model=schemas.SegmentPreviewResponse,
             dependencies=[Depends(get_current_user)])
def preview_segment_users(payload: schemas.SegmentPreviewRequest, db: Session = Depends(get_db)):
    """
    Returns a JSON list of matching users for the chosen segment.
    Use this to let the admin review who will receive the coupon before assigning.
    """
    seg_def = next(
        (s for s in repository.SEGMENT_DEFINITIONS if s["key"] == payload.segmentKey), None
    )
    if not seg_def:
        raise HTTPException(400, f"Unknown segment key: '{payload.segmentKey}'")

    users = repository.get_segment_users(db, payload.segmentKey, payload.direction, payload.count)
    return {
        "segmentKey":   payload.segmentKey,
        "segmentLabel": seg_def["label"],
        "direction":    payload.direction,
        "count":        len(users),
        "users":        users,
    }


@router.post("/UserSegments/GenerateExcel", dependencies=[Depends(get_current_user)])
def generate_segment_excel(payload: schemas.SegmentExcelRequest, db: Session = Depends(get_db)):
    """
    Generates and downloads an Excel file containing the matching user IDs
    for the chosen segment. Admin can then add coupon columns and re-upload,
    OR just use it as-is with AssignFromSegment.

    Excel columns: UserId | Name | Email | Phone | Metric
    """
    seg_def = next(
        (s for s in repository.SEGMENT_DEFINITIONS if s["key"] == payload.segmentKey), None
    )
    if not seg_def:
        raise HTTPException(400, f"Unknown segment key: '{payload.segmentKey}'")

    users = repository.get_segment_users(db, payload.segmentKey, payload.direction, payload.count)
    if not users:
        raise HTTPException(404, "No users found for this segment with the given criteria")

    wb   = openpyxl.Workbook()
    ws   = wb.active
    ws.title = "Users"

    hf, hfill, ha, hb = _header_style()
    df, dfill, db_     = _data_style()
    CENTER = Alignment(horizontal="center", vertical="center")
    LEFT   = Alignment(horizontal="left",   vertical="center")

    # ── Info sheet ────────────────────────────────────────────────────────────
    info = wb.create_sheet("Info")
    info.column_dimensions["A"].width = 70
    BOLD  = Font(name="Calibri", bold=True,  color=BRAND_COLOR, size=12)
    BODY  = Font(name="Calibri",             size=10)
    WRAL  = Alignment(horizontal="left", vertical="center", wrap_text=True)
    rows_ = [
        (f"Segment: {seg_def['label']}",              BOLD),
        (f"Description: {seg_def['description']}",    BODY),
        (f"Direction: {payload.direction.capitalize()} {payload.count}", BODY),
        ("",                                           None),
        ("HOW TO USE THIS FILE",                       Font(name="Calibri", bold=True, size=11)),
        ("Option A – Assign directly in admin:",       BODY),
        ("  Go to User-Specific Coupons → Assign from Segment", BODY),
        ("  Select this same segment, direction and count, fill coupon details and submit.", BODY),
        ("",                                           None),
        ("Option B – Upload with this Excel:",         BODY),
        ("  1. Keep the UserId column exactly as-is.", BODY),
        ("  2. Fill in the coupon columns at the end of this sheet.", BODY),
        ("  3. Upload via User-Specific Coupons → Upload Excel.", BODY),
    ]
    for ri, (txt, fnt) in enumerate(rows_, 1):
        c = info.cell(row=ri, column=1, value=txt)
        if fnt: c.font = fnt
        c.alignment = WRAL
        info.row_dimensions[ri].height = 18

    # ── Users sheet columns ───────────────────────────────────────────────────
    COLS = [
        ("UserId",   40, LEFT),
        ("Name",     28, LEFT),
        ("Email",    34, LEFT),
        ("Phone",    18, CENTER),
        (seg_def["label"], 32, CENTER),   # dynamic metric column
    ]
    ws.row_dimensions[1].height = 24
    for ci, (label, width, align) in enumerate(COLS, 1):
        cell = ws.cell(row=1, column=ci, value=label)
        _apply(cell, hf, hfill, ha, hb)
        ws.column_dimensions[cell.column_letter].width = width

    for ri, user in enumerate(users, 2):
        ws.row_dimensions[ri].height = 18
        row_data = [
            user["userId"],
            user.get("name") or "",
            user.get("email") or "",
            user.get("phone") or "",
            user.get("metricLabel") or "",
        ]
        fill_color = ACCENT_COLOR if ri % 2 == 0 else WHITE
        alt_fill   = PatternFill("solid", fgColor=fill_color)
        thin       = Side(border_style="thin", color="DDDDDD")
        brd        = Border(left=thin, right=thin, top=thin, bottom=thin)
        for ci, (val, (_, _, align)) in enumerate(zip(row_data, COLS), 1):
            cell           = ws.cell(row=ri, column=ci, value=val)
            cell.font      = Font(name="Calibri", size=10)
            cell.fill      = alt_fill
            cell.alignment = align
            cell.border    = brd

    ws.freeze_panes = "A2"

    direction_label = payload.direction
    filename = f"segment_{payload.segmentKey}_{direction_label}{payload.count}_users.xlsx"
    return _excel_response(wb, filename)


# ══════════════════════════════════════════════════════════════════════════════
# USER-SPECIFIC BULK COUPON UPLOAD/ASSIGN  (NEW)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/UserSpecific/Template", dependencies=[Depends(get_current_user)])
def download_user_specific_coupon_template():
    """
    Downloads a sample Excel template that admin fills with:
      UserId | CouponCode | CouponName | Discount | Description | StartDate | EndDate

    The UserId column is the only link to the customer.
    Admin can paste in user IDs from the segment-generated Excel or any other source.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "User Coupons"

    hf, hfill, ha, hb = _header_style()
    sf, sfill, sb      = _sample_style()
    CENTER = Alignment(horizontal="center", vertical="center")
    LEFT   = Alignment(horizontal="left",   vertical="center")

    COLUMNS = [
        ("UserId",      40, LEFT,   "abc123-xxxx-xxxx-xxxx-xxxxxxxxxxxx"),
        ("CouponCode",  22, CENTER, "LOYAL20"),
        ("CouponName",  28, LEFT,   "Loyalty Reward"),
        ("Discount",    14, CENTER, 20),
        ("Description", 40, LEFT,   "20% off for our loyal customers"),
        ("StartDate",   18, CENTER, "2025-08-01"),
        ("EndDate",     18, CENTER, "2025-08-31"),
    ]

    ws.row_dimensions[1].height = 24
    for ci, (label, width, _, _s) in enumerate(COLUMNS, 1):
        cell = ws.cell(row=1, column=ci, value=label)
        _apply(cell, hf, hfill, ha, hb)
        ws.column_dimensions[cell.column_letter].width = width

    ws.row_dimensions[2].height = 20
    for ci, (_, _, align, sample) in enumerate(COLUMNS, 1):
        cell = ws.cell(row=2, column=ci, value=sample)
        _apply(cell, sf, sfill, align, sb)

    ws.freeze_panes = "A2"

    # ── Instructions sheet ────────────────────────────────────────────────────
    info = wb.create_sheet("Instructions")
    info.column_dimensions["A"].width = 72
    BOLD = Font(name="Calibri", bold=True, color=BRAND_COLOR, size=12)
    BODY = Font(name="Calibri", size=10)
    WRAL = Alignment(horizontal="left", vertical="center", wrap_text=True)
    rows_ = [
        ("How to Upload User-Specific Coupons",               BOLD),
        ("",                                                   None),
        ("1. Go to the 'User Coupons' sheet (first tab).",     BODY),
        ("2. Delete the sample row (light pink, row 2).",       BODY),
        ("3. Each row = one user getting a coupon.",            BODY),
        ("4. All users in the file get the coupon you specify per row.", BODY),
        ("   (You can give different discounts to different users.)", BODY),
        ("5. Save and upload via Admin → User-Specific Coupons → Upload Excel.", BODY),
        ("",                                                   None),
        ("Column Guide:",                                      Font(name="Calibri", bold=True, size=10)),
        ("  UserId      (required) — The user's profile ID (UUID string).", BODY),
        ("  CouponCode  (required) — Unique code for this coupon.", BODY),
        ("  CouponName  (required) — Short name shown in the coupon card.", BODY),
        ("  Discount    (required) — Integer percentage (e.g. 20 = 20% off).", BODY),
        ("  Description (optional) — Longer text shown on the coupon.", BODY),
        ("  StartDate   (optional) — Format: YYYY-MM-DD. Leave blank = active now.", BODY),
        ("  EndDate     (optional) — Format: YYYY-MM-DD. Leave blank = never expires.", BODY),
        ("",                                                   None),
        ("Tips:",                                              Font(name="Calibri", bold=True, size=10)),
        ("  • Get UserId values from Segment Generate Excel or admin user list.", BODY),
        ("  • Invalid or non-existent UserIds are silently skipped.", BODY),
        ("  • Each user gets their own personal coupon — not shared.", BODY),
    ]
    for ri, (txt, fnt) in enumerate(rows_, 1):
        c = info.cell(row=ri, column=1, value=txt)
        if fnt: c.font = fnt
        c.alignment = WRAL
        info.row_dimensions[ri].height = 18

    return _excel_response(wb, "user_specific_coupons_template.xlsx")


@router.post(
    "/UserSpecific/Upload",
    response_model=schemas.UserSpecificCouponUploadResponse,
    status_code=201,
    dependencies=[Depends(get_current_user)],
)
async def upload_user_specific_coupons(
    file:    UploadFile = File(...),
    db:      Session    = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Accepts the admin-filled Excel (based on the template above).
    Required columns: UserId, CouponCode, CouponName, Discount.
    Optional: Description, StartDate, EndDate.

    Each row becomes one user-specific coupon.
    All rows in a single upload share the same batchId for audit/rollback.

    NOTE: If rows have different coupon codes / discounts, each unique combination
    is treated as a sub-group. In practice, admins usually keep the same
    coupon settings for everyone in a single upload.
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

    def col(row, name):
        try:
            v = row[headers.index(name.lower())].value
            return v
        except (ValueError, IndexError):
            return None

    required = ["userid", "couponcode", "couponname", "discount"]
    missing  = [c for c in required if c not in headers]
    if missing:
        raise HTTPException(400, f"Missing required columns: {', '.join(missing)}")

    # Parse rows — group by (couponCode, couponName, discount, description, startDate, endDate)
    # so we can do one bulk_assign call per unique coupon definition.
    # In the simple case (all rows same coupon), it's just one call.
    from collections import defaultdict
    groups = defaultdict(list)  # (couponCode, couponName, discount, desc, start, end) → [userId, ...]

    for row in ws.iter_rows(min_row=2):
        user_id = col(row, "userid")
        if not user_id:
            continue
        user_id     = str(user_id).strip()
        coupon_code = str(col(row, "couponcode") or "").strip()
        coupon_name = str(col(row, "couponname") or "").strip()
        discount_v  = col(row, "discount")
        discount    = int(discount_v) if discount_v is not None else None
        description = str(col(row, "description") or "").strip() or None
        start_raw   = col(row, "startdate")
        end_raw     = col(row, "enddate")

        start_date = _parse_date(start_raw)
        end_date   = _parse_date(end_raw)

        key = (coupon_code, coupon_name, discount, description, str(start_date), str(end_date))
        groups[key].append(user_id)

    if not groups:
        raise HTTPException(400, "No valid rows found in the file")

    # If multiple different coupon definitions in same file → multiple batches
    # Return a summary of the last batch (or aggregate)
    total_assigned = 0
    total_skipped  = 0
    last_batch_id  = None

    admin_id = getattr(current_user, "Id", None) or getattr(current_user, "id", None)

    for (coupon_code, coupon_name, discount, description, start_str, end_str), user_ids in groups.items():
        result = repository.bulk_assign_user_specific_coupons(
            db         = db,
            user_ids   = user_ids,
            coupon_data = {
                "couponCode":  coupon_code,
                "couponName":  coupon_name,
                "discount":    discount,
                "description": description,
                "startDate":   None if start_str == "None" else (
                    _parse_date(start_str) if start_str else None
                ),
                "endDate":     None if end_str == "None" else (
                    _parse_date(end_str) if end_str else None
                ),
            },
            source_type     = "excel_upload",
            created_by      = str(admin_id) if admin_id else None,
        )
        total_assigned += result["usersAssigned"]
        total_skipped  += result["usersSkipped"]
        last_batch_id   = result["batchId"]

    return {
        "batchId":       last_batch_id,
        "usersAssigned": total_assigned,
        "usersSkipped":  total_skipped,
        "message":       f"Done: {total_assigned} coupons assigned, {total_skipped} skipped (invalid user IDs)",
    }


@router.post(
    "/UserSpecific/AssignFromSegment",
    response_model=schemas.UserSpecificCouponUploadResponse,
    status_code=201,
    dependencies=[Depends(get_current_user)],
)
def assign_coupon_from_segment(
    payload:      schemas.BulkAssignFromSegmentRequest,
    db:           Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Admin picks a segment + coupon details → coupons are assigned to all
    matching users in one go. No Excel file needed.

    Flow:
      1. Admin views segment preview (POST /UserSegments/Preview)
      2. Admin is satisfied with the list
      3. Admin calls this endpoint with segment + coupon details
      4. All matched users receive a personal coupon
    """
    seg_def = next(
        (s for s in repository.SEGMENT_DEFINITIONS if s["key"] == payload.segmentKey), None
    )
    if not seg_def:
        raise HTTPException(400, f"Unknown segment key: '{payload.segmentKey}'")

    users = repository.get_segment_users(db, payload.segmentKey, payload.direction, payload.count)
    if not users:
        raise HTTPException(404, "No users found for this segment")

    user_ids = [u["userId"] for u in users]
    admin_id = getattr(current_user, "Id", None) or getattr(current_user, "id", None)

    result = repository.bulk_assign_user_specific_coupons(
        db          = db,
        user_ids    = user_ids,
        coupon_data = {
            "couponCode":  payload.couponCode,
            "couponName":  payload.couponName,
            "discount":    payload.discount,
            "description": payload.description,
            "startDate":   payload.startDate,
            "endDate":     payload.endDate,
        },
        source_type       = "segment_generate",
        created_by        = str(admin_id) if admin_id else None,
        segment_key       = payload.segmentKey,
        segment_direction = payload.direction,
    )

    return {
        "batchId":       result["batchId"],
        "usersAssigned": result["usersAssigned"],
        "usersSkipped":  result["usersSkipped"],
        "message": (
            f"Assigned '{payload.couponName}' to {result['usersAssigned']} users "
            f"from segment '{seg_def['label']}' ({payload.direction} {payload.count}). "
            f"{result['usersSkipped']} skipped (not found)."
        ),
    }


@router.get(
    "/UserSpecific/Batches",
    response_model=schemas.UserCouponBatchListResponse,
    dependencies=[Depends(get_current_user)],
)
def list_user_coupon_batches(
    Page_Index: Optional[int] = Query(None, alias="Page.Index", ge=1),
    Page_Size:  Optional[int] = Query(None, alias="Page.Size",  ge=1),
    db: Session = Depends(get_db),
):
    """
    Admin: view history of all bulk user-specific coupon assignments.
    Shows who was targeted, what coupon was given, and how many users.
    """
    result = repository.get_user_coupon_batches(db, Page_Index, Page_Size)
    return {"count": result["count"], "list": result["list"]}


@router.put(
    "/UserSpecific/Batches/{batch_id}/Deactivate",
    status_code=200,
    dependencies=[Depends(get_current_user)],
)
def deactivate_user_coupon_batch(batch_id: int, db: Session = Depends(get_db)):
    """
    Deactivates all coupons in a batch (e.g., if admin made a mistake).
    Users will no longer see these coupons in their profile/cart.
    Already-used coupons are NOT reversed (that would require order adjustments).
    """
    success = repository.deactivate_batch(db, batch_id)
    if not success:
        raise HTTPException(404, f"Batch {batch_id} not found")
    return {"success": True, "message": f"Batch {batch_id} and all its coupons have been deactivated"}


# ─── Private helpers ──────────────────────────────────────────────────────────

def _parse_date(value):
    """Safely parse a date value from an Excel cell (string, date, datetime or None)."""
    if value is None:
        return None
    if hasattr(value, "date"):   # already a date/datetime object from openpyxl
        return value
    s = str(value).strip()
    if not s or s.lower() in ("none", "n/a", "-", ""):
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            from datetime import datetime as _dt
            return _dt.strptime(s, fmt)
        except ValueError:
            continue
    return None

# ══════════════════════════════════════════════════════════════════════════════
# BRAND COUPON ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/AdminList", summary="Unified coupon list for admin Coupon List page")
def admin_coupons_list(
    status:     Optional[str] = Query(None),   # all | active | used | inactive
    search:     Optional[str] = Query(None),
    page_index: int = Query(1, alias="Page.Index", ge=1),
    page_size:  int = Query(30, alias="Page.Size",  ge=1),
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    return repository.get_admin_coupons_list(
        db,
        status     = status,
        search     = search,
        page_index = page_index,
        page_size  = page_size,
    )


@router.get("/Brand", summary="List brand coupons (admin)")
def list_brand_coupons(
    brand_id:   Optional[int] = Query(None),
    status:     Optional[str] = Query(None),  # "active" | "used" | "inactive" | None=all
    page_index: int = Query(1, alias="Page.Index", ge=1),
    page_size:  int = Query(20, alias="Page.Size",  ge=1),
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    result = repository.get_brand_coupons(
        db, brand_id=brand_id, status=status,
        page_index=page_index, page_size=page_size,
    )
    return {
        "list":  [_brand_coupon_dict(c) for c in result["list"]],
        "count": result["count"],
    }


@router.get("/Brand/UsageDetails", summary="Brand coupon usage — who used which code (admin)")
def brand_coupon_usage_details(
    brand_id:   Optional[int] = Query(None),
    page_index: int = Query(1, alias="Page.Index", ge=1),
    page_size:  int = Query(50, alias="Page.Size",  ge=1),
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    return repository.get_brand_coupon_usage_details(
        db, brand_id=brand_id, page_index=page_index, page_size=page_size,
    )


@router.post("/Brand/Single", summary="Create a single brand coupon (admin)")
def create_single_brand_coupon(
    payload: dict,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Body: { couponCode, couponName, brandId, discount, startDate?, endDate? }
    Creates one single-use brand coupon.
    """
    try:
        c = repository.create_brand_coupon(
            db,
            coupon_code  = payload["couponCode"],
            coupon_name  = payload.get("couponName", ""),
            brand_id     = int(payload["brandId"]),
            discount     = int(payload["discount"]),
            start_date   = _parse_date(payload.get("startDate")),
            end_date     = _parse_date(payload.get("endDate")),
            created_by   = getattr(current_user, "user_id", None),
        )
        return {"success": True, "coupon": _brand_coupon_dict(c)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/Brand/BulkUpload", summary="Bulk upload brand coupons from Excel (admin)")
async def bulk_upload_brand_coupons(
    file:        UploadFile = File(...),
    couponName:  str        = Query(...),
    brandId:     int        = Query(...),
    discount:    int        = Query(...),
    startDate:   Optional[str] = Query(None),
    endDate:     Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Accepts an Excel file with a single column of coupon codes (no header needed,
    or header row is auto-skipped if first cell is not a valid code).
    Query params: couponName, brandId, discount, startDate?, endDate?
    """
    content = await file.read()
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
    ws = wb.active
    codes = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        val = row[0] if row else None
        if val is None:
            continue
        code = str(val).strip().upper()
        # Skip obvious header rows
        if i == 0 and code in ("CODE", "COUPON CODE", "COUPONCODE", "CODES"):
            continue
        if code:
            codes.append(code)
    wb.close()

    if not codes:
        raise HTTPException(400, "No valid coupon codes found in uploaded file")

    inserted, skipped = repository.bulk_create_brand_coupons(
        db,
        codes        = codes,
        coupon_name  = couponName,
        brand_id     = brandId,
        discount     = discount,
        start_date   = _parse_date(startDate),
        end_date     = _parse_date(endDate),
        created_by   = getattr(current_user, "user_id", None),
    )
    return {
        "success":  True,
        "inserted": inserted,
        "skipped":  skipped,
        "message":  f"{inserted} coupon(s) created, {skipped} skipped (already existed or blank)",
    }


@router.get("/Brand/Template", summary="Download Excel template for brand coupon bulk upload")
def brand_coupon_template(_: dict = Depends(get_current_user)):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Brand Coupons"
    ws.column_dimensions["A"].width = 22
    bold_font = Font(bold=True, color="FFFFFF")
    fill = PatternFill("solid", fgColor="6B0F2A")
    ws["A1"] = "Coupon Code"
    ws["A1"].font = bold_font
    ws["A1"].fill = fill
    # Sample rows
    for sample in ["BRAND001", "BRAND002", "BRAND003"]:
        ws.append([sample])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=brand_coupon_template.xlsx"},
    )


@router.post("/Brand/Validate", summary="Validate a brand coupon (checkout)")
def validate_brand_coupon(
    payload: dict,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Body: { couponCode, brandId, userProfileId }
    Used at checkout to check if a brand coupon is valid for the current cart.
    Returns: { valid, couponId, discount, brandId, message }
    """
    result = repository.validate_brand_coupon(
        db,
        coupon_code     = payload.get("couponCode", ""),
        brand_id        = int(payload.get("brandId", 0)),
        user_profile_id = payload.get("userProfileId", ""),
    )
    return result


@router.post("/Brand/MarkUsed", summary="Mark a brand coupon used after successful order")
def mark_brand_coupon_used(
    payload: dict,
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    """
    Body: { couponId, userProfileId, orderId? }
    Call after createOrder succeeds when a brand coupon was applied.
    """
    try:
        repository.mark_brand_coupon_used(
            db,
            coupon_id       = int(payload["couponId"]),
            user_profile_id = payload["userProfileId"],
            order_id        = payload.get("orderId"),
        )
        return {"success": True}
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.delete("/Brand/{coupon_id}", summary="Delete a brand coupon (admin)")
def delete_brand_coupon(
    coupon_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    from datetime import datetime, timezone
    from .models import Coupons
    c = db.query(Coupons).filter(
        Coupons.couponId == coupon_id,
        Coupons.couponType == "brand_specific",
        Coupons.deletedInd == False,
    ).first()
    if not c:
        raise HTTPException(404, "Brand coupon not found")
    c.deletedInd   = True
    c.modifiedDate = datetime.now(timezone.utc)
    db.add(c)
    db.commit()
    return {"success": True}


# ── helpers ────────────────────────────────────────────────────────────────────

def _brand_coupon_dict(c) -> dict:
    return {
        "couponId":   c.couponId,
        "couponCode": c.couponCode,
        "couponName": c.couponName,
        "brandId":    c.brandId,
        "discount":   c.discount,
        "startDate":  c.startDate.isoformat() if c.startDate else None,
        "endDate":    c.endDate.isoformat()   if c.endDate   else None,
        "isActive":   c.isActive,
        "isUsed":     c.isUsed,
        "isSingleUse": c.isSingleUse,
        "createdDate": c.createdDate.isoformat() if c.createdDate else None,
    }


def _parse_date(v):
    if not v:
        return None
    from datetime import datetime, timezone
    try:
        return datetime.fromisoformat(v).replace(tzinfo=timezone.utc)
    except Exception:
        try:
            return datetime.strptime(v, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            return None