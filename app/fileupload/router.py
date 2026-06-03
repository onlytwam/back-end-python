"""
app/fileupload/router.py
────────────────────────────────────────────────────────────────────────────
GET  /api/FileUpload        — list all uploads (paginated)
POST /api/FileUpload        — JSON create (legacy — kept for back-compat)
POST /api/FileUpload/upload — multipart file upload (used by Contact Us, etc.)
                              Returns { url, fileUrl, fileUploadId }
"""
import json
from typing import Optional

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy.orm import Session

from database import get_db
from . import schemas, repository
from app.auth.dependencies import get_current_user, CurrentUser
from app.shared.azure_storage import upload_file
from app.shared.url_helpers import resolve_url

router = APIRouter(
    dependencies=[Depends(get_current_user)],
    prefix="/api/FileUpload",
    tags=["FileUpload"],
)


# ── List all uploads ──────────────────────────────────────────────────────────
@router.get("", response_model=schemas.FileUploadListResponse)
def get_list(
    Filters:          Optional[str]  = Query(None, alias="Filters"),
    Order_Ascending:  Optional[bool] = Query(None, alias="Order.Ascending"),
    Order_Property:   Optional[str]  = Query(None, alias="Order.Property"),
    Page_Index:       Optional[int]  = Query(None, alias="Page.Index", ge=1),
    Page_Size:        Optional[int]  = Query(None, alias="Page.Size",  ge=1),
    db: Session = Depends(get_db),
):
    return repository.get_all(
        db,
        json.loads(Filters) if Filters else None,
        Order_Ascending, Order_Property, Page_Index, Page_Size,
    )


# ── Legacy JSON create ────────────────────────────────────────────────────────
@router.post("", status_code=201)
def create(command: schemas.FileUploadCreate, db: Session = Depends(get_db)):
    return {"fileUploadId": repository.create(db, command)}


# ── Multipart file upload (Contact Us / general attachments) ─────────────────
@router.post("/upload", status_code=201)
async def upload(
    file: UploadFile = File(...),
    db:   Session    = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Accept an image or PDF, upload it to Azure Blob Storage (or local disk in
    dev), persist a FileUpload record, and return the public URL.

    Frontend usage (Contact Us page):
        const res = await api.post("/FileUpload/upload", formData);
        const url = res.data.url;
    """
    # Upload to Azure / local — raises 400/500 on errors
    # Stored in the dedicated contactus container so contact-us attachments
    # are kept separate from all other media.
    file_url = upload_file(file, container_key="contactus")

    # Persist a record so admins can audit what was uploaded
    record_id = repository.create_from_upload(
        db,
        file_name=file.filename or "upload",
        file_url=file_url,
        file_type=file.content_type or "application/octet-stream",
        created_by=getattr(current_user, "user_id", None),
    )

    resolved_url = resolve_url(file_url) or ""

    return {
        "fileUploadId": record_id,
        "url":          resolved_url,   # ← primary key the frontend reads
        "fileUrl":      resolved_url,   # ← alias for back-compat
    }
