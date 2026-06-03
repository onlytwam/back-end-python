"""
app/fileupload/repository.py
────────────────────────────────────────────────────────────────────────────
Handles DB persistence for FileUpload records.
The actual blob upload is done by app/shared/azure_storage.py; this module
only persists metadata and handles the legacy base-64 JSON path.
"""
import os
import base64
import uuid
from datetime import datetime, timezone

from sqlalchemy import asc, desc
from sqlalchemy.orm import Session

from .models import FileUpload
from app.shared.azure_storage import upload_bytes
from app.shared.url_helpers import resolve_url, storage_path_for_db

BASE_URL = os.getenv("BASE_URL", "")


def _to_dict(r: FileUpload) -> dict:
    file_url = resolve_url(r.filePath)
    return {
        "fileUploadId": r.fileUploadId,
        "fileName":     r.fileName,
        "filePath":     file_url,
        "fileType":     r.fileType,
        "fileUrl":      file_url,
        "createdDate":  r.createdDate,
    }


def get_all(db: Session, filters, order_ascending, order_property, page_index, page_size):
    query = db.query(FileUpload).filter(FileUpload.deletedInd == False)

    if filters:
        for f in filters:
            col = getattr(FileUpload, f.get("property", ""), None)
            if col is not None:
                query = query.filter(col == f.get("value"))

    if order_property:
        col = getattr(FileUpload, order_property, None)
        if col:
            query = query.order_by(asc(col) if order_ascending is not False else desc(col))

    total = query.count()
    if page_index and page_size:
        query = query.offset((page_index - 1) * page_size).limit(page_size)

    return {"count": total, "list": [_to_dict(r) for r in query.all()], "parameters": None}


def get_by_id(db: Session, file_upload_id: int):
    r = db.query(FileUpload).filter(
        FileUpload.fileUploadId == file_upload_id,
        FileUpload.deletedInd == False,
    ).first()
    return _to_dict(r) if r else None


def create(db: Session, data) -> int:
    """
    Legacy JSON endpoint: accepts base-64 fileData or a pre-existing filePath.
    Uploads bytes to Azure / local and persists the record.
    """
    file_path = storage_path_for_db(data.filePath)

    if data.fileData and data.fileName:
        try:
            file_bytes   = base64.b64decode(data.fileData)
            content_type = data.fileType or "application/octet-stream"
            file_path    = upload_bytes(
                file_bytes=file_bytes,
                filename=data.fileName,
                content_type=content_type,
                container_key="contactus",
            )
        except Exception as exc:
            print(f"[FileUpload] Upload failed: {exc}")

    entity = FileUpload(
        fileName    = data.fileName,
        filePath    = file_path,
        fileType    = data.fileType,
        createdDate = datetime.now(timezone.utc),
        deletedInd  = False,
    )
    db.add(entity)
    db.commit()
    db.refresh(entity)
    return entity.fileUploadId


def create_from_upload(
    db: Session,
    file_name: str,
    file_url: str,
    file_type: str,
    created_by: str | None = None,
) -> int:
    """
    Persist a FileUpload record after the file has already been uploaded to
    Azure / local by the router.  Returns the new fileUploadId.
    """
    entity = FileUpload(
        fileName    = file_name,
        filePath    = storage_path_for_db(file_url),
        fileType    = file_type,
        createdDate = datetime.now(timezone.utc),
        deletedInd  = False,
        createdBy   = created_by,
    )
    db.add(entity)
    db.commit()
    db.refresh(entity)
    return entity.fileUploadId
