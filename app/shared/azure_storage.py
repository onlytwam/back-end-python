"""


app/shared/azure_storage.py


───────────────────────────────────────────────────────────────────────────────


Single source-of-truth for ALL Azure Blob Storage operations across the app.





Environment variables (set in Azure App Service → Configuration):


  AZURE_STORAGE_CONNECTION_STRING   — required on Azure; absent = local fallback


  AZURE_STORAGE_ACCOUNT_NAME        — optional override (default: twamstorage)





Container layout (auto-created if they don't exist):


  products   ← product / variant images  (sub-folder per product name)


  category   ← category banner / hero images (sub-folder per category id)


  brands     ← brand logos and banner images


  blogs      ← blog / article images


  profiles   ← user profile images (sub-folder per user id)


  reviews    ← customer review photos (sub-folder per product id)


  contactus  ← contact-us / support attachments


  home       ← home page images, hero video, and login background





Local fallback (when connection string is absent):


  Files are saved to  <project_root>/uploads/<container_name>/<blob_name>


  Served via FastAPI StaticFiles at  /uploads/…


  (Same 8-folder structure mirrors Azure exactly.)


"""





import io


import os


import re


import uuid


from pathlib import Path


from typing import Optional





from fastapi import HTTPException, UploadFile





# ── Azure SDK — imported lazily so the app still boots without the package ───


try:


    from azure.storage.blob import BlobServiceClient, ContentSettings


    _AZURE_AVAILABLE = True


except ImportError:


    BlobServiceClient = None        # type: ignore


    ContentSettings   = None        # type: ignore


    _AZURE_AVAILABLE  = False





# ── Config ───────────────────────────────────────────────────────────────────


AZURE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")


def _account_name_from_connection_string(connection_string: str) -> Optional[str]:
    for part in connection_string.split(";"):
        key, _, value = part.partition("=")
        if key.strip().lower() == "accountname" and value.strip():
            return value.strip()
    return None


AZURE_ACCOUNT_NAME      = (
    os.getenv("AZURE_STORAGE_ACCOUNT_NAME")
    or _account_name_from_connection_string(AZURE_CONNECTION_STRING)
    or "twamstorage"
)


AZURE_BASE_URL          = f"https://{AZURE_ACCOUNT_NAME}.blob.core.windows.net"





# Project root — used only for local fallback paths


_HERE     = Path(__file__).resolve()


_APP_ROOT = _HERE.parent.parent.parent          # …/app/shared/azure_storage.py → project root


LOCAL_UPLOAD_ROOT = _APP_ROOT / "uploads"





# ── Container registry ────────────────────────────────────────────────────────


# Add new containers here; they are created automatically on first use.


CONTAINERS: dict[str, str] = {
    "products":   "products",   # product / variant images  (sub-folder per product name)
    "category":   "category",   # category banner / hero images (sub-folder per category id)
    "brands":     "brands",     # brand logos / banners
    "blogs":      "blogs",      # blog / article images
    "profiles":   "profiles",   # user profile pictures (sub-folder per user id)
    "reviews":    "reviews",    # customer review photos (sub-folder per product id)
    "contactus":  "contactus",  # contact-us / support attachments
    "home":       "home",       # home page images, hero video & login background
}





# Mime types accepted for image-only endpoints


ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/jpg"}





# Mime types accepted for page-level media slots (hero video, section images, etc.)


# Videos are only allowed here — not on product/brand/review image endpoints.


ALLOWED_VIDEO_TYPES = {"video/mp4", "video/webm"}


ALLOWED_SITE_MEDIA_TYPES = ALLOWED_IMAGE_TYPES | ALLOWED_VIDEO_TYPES





# Mime types accepted for the general-purpose upload endpoint (contact us, etc.)


ALLOWED_UPLOAD_TYPES = ALLOWED_IMAGE_TYPES | {"application/pdf"}








# ── Helpers ───────────────────────────────────────────────────────────────────





def _is_azure_mode() -> bool:


    """True when we have a valid connection string and the SDK is installed."""


    return bool(AZURE_CONNECTION_STRING and _AZURE_AVAILABLE)








def _safe_blob_name(filename: str, prefix: str = "") -> str:


    """Return a collision-free, filesystem-safe blob name."""


    if "." in filename:


        stem, ext = filename.rsplit(".", 1)


        ext = ext.lower()


    else:


        stem, ext = filename, "jpg"





    if ext not in ("jpg", "jpeg", "png", "webp", "gif", "pdf", "mp4", "webm"):


        ext = "jpg"





    stem = re.sub(r"[^a-zA-Z0-9_-]", "_", stem)[:40].strip("_") or "file"


    unique = f"{stem}_{uuid.uuid4().hex[:8]}.{ext}"


    return f"{prefix.rstrip('/')}/{unique}".lstrip("/") if prefix else unique








def _ensure_container(blob_service: "BlobServiceClient", container_name: str) -> None:
    """Create the container if it does not exist.

    Some Azure storage accounts disable anonymous public access at the account
    level. In those environments, create_container(public_access="blob") fails
    with PublicAccessNotPermitted even though private uploads are allowed.
    """
    container_client = blob_service.get_container_client(container_name)

    if container_client.exists():
        return

    try:
        container_client.create_container(public_access="blob")
        print(f"[AzureBlob] Created public container: {container_name}")
        return
    except Exception as exc:
        msg = str(exc).lower()

        if "containeralreadyexists" in msg or "already exists" in msg:
            return

        if "publicaccessnotpermitted" not in msg:
            raise

        print(
            f"[AzureBlob] Public access disabled; creating private container: {container_name}"
        )

    try:
        container_client.create_container()
        print(f"[AzureBlob] Created private container: {container_name}")
    except Exception as exc:
        msg = str(exc).lower()
        if "containeralreadyexists" in msg or "already exists" in msg:
            return
        raise


def _ensure_container_legacy_unused(blob_service: "BlobServiceClient", container_name: str) -> None:


    """Create the container (with public blob read access) if it does not exist."""


    try:


        container_client = blob_service.get_container_client(container_name)


        container_client.create_container(public_access="blob")


        print(f"[AzureBlob] Created container: {container_name}")


    except Exception as exc:


        # ResourceExistsError is the normal case — ignore it


        msg = str(exc).lower()


        if "containeralreadyexists" in msg or "already exists" in msg:


            return          # container already there, all good


        # Any other error (auth, network…) — re-raise


        raise








def _local_fallback_save(file_bytes: bytes, container_name: str, blob_name: str) -> str:


    """Save to local disk and return a URL-safe path for development."""


    dest_dir = LOCAL_UPLOAD_ROOT / container_name / Path(blob_name).parent


    dest_dir.mkdir(parents=True, exist_ok=True)


    dest_file = LOCAL_UPLOAD_ROOT / container_name / blob_name


    dest_file.write_bytes(file_bytes)


    return f"/uploads/{container_name}/{blob_name}"








def _local_fallback_delete(url_path: str) -> bool:


    """Remove a local fallback file given its URL-safe relative path."""


    # url_path looks like /uploads/<container>/<blob>


    if not url_path.startswith("/uploads/"):


        return False


    relative = url_path.lstrip("/")            # uploads/<container>/<blob>


    abs_path  = _APP_ROOT / relative


    try:


        if abs_path.exists() and abs_path.is_file():


            abs_path.unlink()


            print(f"[LocalStorage] Deleted: {abs_path}")


        return True


    except OSError as exc:


        print(f"[LocalStorage] Delete failed: {exc}")


        return False








# ── Public API ────────────────────────────────────────────────────────────────





def upload_image(


    file: UploadFile,


    container_key: str,


    folder: str = "",


) -> str:


    """


    Upload an image to Azure Blob (or local disk in dev).





    Args:


        file:          FastAPI UploadFile


        container_key: key from CONTAINERS dict  e.g. "reviews", "products"


        folder:        optional subfolder path   e.g. "thumbnails"





    Returns:


        Full public URL (Azure) or local path (/uploads/…) that can be stored


        in the DB and returned to the frontend.





    Raises:


        HTTPException 400 for invalid file type / empty file


        HTTPException 500 for storage errors


    """


    if file.content_type not in ALLOWED_IMAGE_TYPES:


        raise HTTPException(


            status_code=400,


            detail=(


                f"Unsupported image type '{file.content_type}'. "


                "Allowed: jpeg, png, webp, gif."


            ),


        )


    return _do_upload(file, container_key, folder, allowed_types=ALLOWED_IMAGE_TYPES)








def upload_file(


    file: UploadFile,


    container_key: str = "uploads",


    folder: str = "",


) -> str:


    """


    Upload any allowed file (image or PDF) — used by general endpoints such


    as Contact-Us / support-request attachments.





    Returns the public URL / local path identical to upload_image().


    """


    if file.content_type not in ALLOWED_UPLOAD_TYPES:


        raise HTTPException(


            status_code=400,


            detail=(


                f"Unsupported file type '{file.content_type}'. "


                "Allowed: jpeg, png, webp, gif, pdf."


            ),


        )


    return _do_upload(file, container_key, folder, allowed_types=ALLOWED_UPLOAD_TYPES)








def upload_media(


    file: UploadFile,


    container_key: str,


    folder: str = "",


) -> str:


    """


    Upload an image OR a video file — used exclusively by page-image slots


    such as the home hero video.  All other image endpoints continue to use


    upload_image() which does not allow video.





    Returns the public URL / local path identical to upload_image().


    """


    if file.content_type not in ALLOWED_SITE_MEDIA_TYPES:


        raise HTTPException(


            status_code=400,


            detail=(


                f"Unsupported file type '{file.content_type}'. "


                "Allowed: jpeg, png, webp, gif, mp4, webm."


            ),


        )


    return _do_upload(file, container_key, folder, allowed_types=ALLOWED_SITE_MEDIA_TYPES)








def upload_bytes(


    file_bytes: bytes,


    filename: str,


    content_type: str,


    container_key: str,


    folder: str = "",


) -> str:


    """


    Upload raw bytes (e.g. decoded base-64 profile images).





    Returns the public URL / local path.


    """


    container_name = _resolve_container(container_key)


    blob_name      = _safe_blob_name(filename, folder)





    print(f"[AzureBlob] Uploading bytes ({len(file_bytes)} B) → {container_name}/{blob_name}")





    if _is_azure_mode():


        try:


            blob_service = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)


            _ensure_container(blob_service, container_name)


            blob_client = blob_service.get_blob_client(container=container_name, blob=blob_name)


            blob_client.upload_blob(


                io.BytesIO(file_bytes),


                overwrite=True,


                content_settings=ContentSettings(content_type=content_type),


            )


            relative_path = f"{container_name}/{blob_name}"


            print(f"[AzureBlob] Uploaded: {AZURE_BASE_URL}/{relative_path}")


            return relative_path


        except HTTPException:


            raise


        except Exception as exc:


            print(f"[AzureBlob] Upload failed: {type(exc).__name__}: {exc}")


            raise HTTPException(status_code=500, detail=f"Azure upload failed: {exc}") from exc





    # ── Local fallback ────────────────────────────────────────────────────────


    path = _local_fallback_save(file_bytes, container_name, blob_name)


    print(f"[LocalStorage] Saved: {path}")


    return path








def delete_image(blob_url: str) -> bool:


    """


    Delete a blob by its full URL (Azure) or relative path (/uploads/…).


    Safe to call with None / empty — returns False without raising.


    """


    if not blob_url:


        return False





    if not _is_azure_mode():


        return _local_fallback_delete(blob_url)





    # Azure path


    # Handle relative path like "products/folder/file.jpg" (new format)
    if not blob_url.startswith("http://") and not blob_url.startswith("https://") and not blob_url.startswith("/uploads/"):
        parts = blob_url.lstrip("/").split("/", 1)
        if len(parts) == 2:
            try:
                blob_service = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
                blob_client  = blob_service.get_blob_client(container=parts[0], blob=parts[1])
                blob_client.delete_blob()
                print(f"[AzureBlob] Deleted relative: {blob_url}")
                return True
            except Exception as exc:
                print(f"[AzureBlob] Delete failed: {exc}")
                return False
        return False

    if not blob_url.startswith(AZURE_BASE_URL):


        # Could be a local path stored before migration — try local delete


        return _local_fallback_delete(blob_url)





    try:


        suffix      = blob_url[len(AZURE_BASE_URL):].lstrip("/")


        parts       = suffix.split("/", 1)


        if len(parts) != 2:


            return False


        container_name, blob_name = parts





        blob_service = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)


        blob_client  = blob_service.get_blob_client(container=container_name, blob=blob_name)


        blob_client.delete_blob()


        print(f"[AzureBlob] Deleted: {blob_url}")


        return True


    except Exception as exc:


        print(f"[AzureBlob] Delete failed: {exc}")


        return False








# ── Internal helpers ──────────────────────────────────────────────────────────





def _resolve_container(key: str) -> str:


    name = CONTAINERS.get(key)


    if not name:


        raise HTTPException(


            status_code=500,


            detail=f"Unknown container key '{key}'. Available: {list(CONTAINERS)}",


        )


    return name








def _do_upload(


    file: UploadFile,


    container_key: str,


    folder: str,


    allowed_types: set,


) -> str:


    container_name = _resolve_container(container_key)


    blob_name      = _safe_blob_name(file.filename or "upload", folder)





    file_bytes = file.file.read()


    if not file_bytes:


        raise HTTPException(status_code=400, detail="Uploaded file is empty.")





    print(


        f"[AzureBlob] Uploading {file.filename!r} "


        f"({len(file_bytes)} B) → {container_name}/{blob_name}"


    )





    if _is_azure_mode():


        try:


            blob_service = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)


            _ensure_container(blob_service, container_name)


            blob_client = blob_service.get_blob_client(container=container_name, blob=blob_name)


            blob_client.upload_blob(


                io.BytesIO(file_bytes),


                overwrite=True,


                content_settings=ContentSettings(content_type=file.content_type),


            )


            relative_path = f"{container_name}/{blob_name}"


            print(f"[AzureBlob] Uploaded: {AZURE_BASE_URL}/{relative_path}")


            return relative_path


        except HTTPException:


            raise


        except Exception as exc:


            print(f"[AzureBlob] Upload failed: {type(exc).__name__}: {exc}")


            raise HTTPException(status_code=500, detail=f"Azure upload failed: {exc}") from exc





    # ── Local fallback ────────────────────────────────────────────────────────


    path = _local_fallback_save(file_bytes, container_name, blob_name)


    print(f"[LocalStorage] Saved: {path}")


    return path
