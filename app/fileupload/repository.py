import os, base64, uuid
from datetime import datetime, timezone
from sqlalchemy import asc, desc
from sqlalchemy.orm import Session
from .models import FileUpload

BASE_URL = os.getenv("BASE_URL", "")
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")
AZURE_STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")
AZURE_STORAGE_CONTAINER = os.getenv("AZURE_STORAGE_CONTAINER", "product-images")


def _to_dict(r: FileUpload) -> dict:
    return {
        "fileUploadId": r.fileUploadId,
        "fileName": r.fileName,
        "filePath": r.filePath,
        "fileType": r.fileType,
        "fileUrl": f"{BASE_URL}/{AZURE_STORAGE_CONTAINER}{r.filePath}" if r.filePath and AZURE_STORAGE_CONNECTION_STRING
                   else f"{BASE_URL}{r.filePath}" if r.filePath else None,
        "createdDate": r.createdDate,
    }


def _save_file(file_bytes: bytes, file_name: str) -> str:
    safe_name = os.path.basename(file_name)
    unique_name = f"{uuid.uuid4().hex[:8]}_{safe_name}"

    if AZURE_STORAGE_CONNECTION_STRING:
        try:
            from azure.storage.blob import BlobServiceClient
            blob_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
            container = blob_client.get_container_client(AZURE_STORAGE_CONTAINER)
            container.upload_blob(unique_name, file_bytes, overwrite=True)
            return f"/{unique_name}"
        except Exception as e:
            print(f"[FileUpload] Azure Blob upload failed, falling back to disk: {e}")

    # Fallback: local disk (for local dev)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    disk_path = os.path.join(UPLOAD_DIR, unique_name)
    with open(disk_path, "wb") as f:
        f.write(file_bytes)
    return f"/{UPLOAD_DIR}/{unique_name}"


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
        FileUpload.deletedInd == False
    ).first()
    return _to_dict(r) if r else None


def create(db: Session, data) -> int:
    file_path = data.filePath
    if data.fileData and data.fileName:
        try:
            file_bytes = base64.b64decode(data.fileData)
            file_path = _save_file(file_bytes, data.fileName)
        except Exception as e:
            print(f"[FileUpload] Failed to save file: {e}")

    entity = FileUpload(
        fileName=data.fileName, filePath=file_path,
        fileType=data.fileType, createdDate=datetime.now(timezone.utc), deletedInd=False
    )
    db.add(entity)
    db.commit()
    db.refresh(entity)
    return entity.fileUploadId