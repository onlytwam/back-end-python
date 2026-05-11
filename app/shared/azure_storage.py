import os
import re
import uuid
from fastapi import UploadFile, HTTPException
from azure.storage.blob import BlobServiceClient, ContentSettings

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

# ── Single storage account — shared across all modules ─────────────────────
AZURE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
AZURE_ACCOUNT_NAME      = "twamstorage"
AZURE_BASE_URL          = f"https://{AZURE_ACCOUNT_NAME}.blob.core.windows.net"

# ── Container registry — add new containers here as needed ─────────────────
CONTAINERS = {
    "reviews":  "reviews",        # review photos
    "products": "product-images", # product images
    # "profiles": "profiles",       # user profile pictures (future)
    # "blogs":    "blogs",          # blog images (future)
}


def upload_image(
    file: UploadFile,
    container_key: str,          # e.g. "reviews", "products"
    folder: str = "",            # optional subfolder e.g. "thumbnails/"
) -> str:
    """
    Upload an image to Azure Blob Storage.
    
    Args:
        file:          FastAPI UploadFile
        container_key: key from CONTAINERS dict
        folder:        optional subfolder within container
    
    Returns:
        Full public URL of the uploaded blob
    
    Usage:
        from app.shared.azure_storage import upload_image
        url = upload_image(file, container_key="reviews")
        url = upload_image(file, container_key="products", folder="thumbnails/")
    """

    # ── Validate image type ────────────────────────────────────────────────
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported image type '{file.content_type}'. "
                   f"Allowed: jpeg, png, webp, gif."
        )

    # ── Validate connection string ─────────────────────────────────────────
    if not AZURE_CONNECTION_STRING:
        raise HTTPException(
            status_code=500,
            detail="AZURE_STORAGE_CONNECTION_STRING is not configured."
        )

    # ── Resolve container name ─────────────────────────────────────────────
    container_name = CONTAINERS.get(container_key)
    if not container_name:
        raise HTTPException(
            status_code=500,
            detail=f"Unknown container key '{container_key}'. "
                   f"Available: {list(CONTAINERS.keys())}"
        )

    # ── Build blob name ────────────────────────────────────────────────────
    ext = file.filename.rsplit(".", 1)[-1] if "." in file.filename else "jpg"
    if ext not in ["jpg", "jpeg", "png", "webp", "gif"]:
        ext = "jpg"
    original_name = file.filename.rsplit(".", 1)[0] if "." in file.filename else file.filename
    original_name = re.sub(r'[^a-zA-Z0-9_-]', '_', original_name) 
    original_name = original_name[:30].strip('_')                   

    blob_name = f"{folder}{original_name}_{uuid.uuid4().hex[:8]}.{ext}"

    # ── Read file bytes ────────────────────────────────────────────────────
    file_bytes = file.file.read()
    print(f"[AZURE]  Uploading {file.filename} ({len(file_bytes)} bytes) "
          f"→ {container_name}/{blob_name}")

    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    # ── Upload to Azure ────────────────────────────────────────────────────
    try:
        blob_service = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
        blob_client  = blob_service.get_blob_client(
            container=container_name,
            blob=blob_name
        )
        blob_client.upload_blob(
            file_bytes,
            overwrite=True,
            content_settings=ContentSettings(content_type=file.content_type)
        )
        url = f"{AZURE_BASE_URL}/{container_name}/{blob_name}"
        print(f"[AZURE]  Uploaded: {url}")
        return url

    except Exception as e:
        print(f"[AZURE]  Failed: {type(e).__name__}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Azure upload failed: {str(e)}"
        )


def delete_image(blob_url: str) -> bool:
    """
    Delete a blob by its full URL.
    Useful when updating/replacing an existing image.
    
    Usage:
        from app.shared.azure_storage import delete_image
        delete_image(old_photo_url)
    """
    if not blob_url or not AZURE_CONNECTION_STRING:
        return False

    try:
        # Extract container and blob name from URL
        # URL format: https://twamstorage.blob.core.windows.net/container/blob
        parts = blob_url.replace(f"{AZURE_BASE_URL}/", "").split("/", 1)
        if len(parts) != 2:
            return False

        container_name, blob_name = parts

        blob_service = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
        blob_client  = blob_service.get_blob_client(
            container=container_name,
            blob=blob_name
        )
        blob_client.delete_blob()
        print(f"[AZURE]  Deleted: {blob_url}")
        return True

    except Exception as e:
        print(f"[AZURE]  Delete failed: {e}")
        return False