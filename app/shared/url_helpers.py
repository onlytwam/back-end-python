"""
app/shared/url_helpers.py
─────────────────────────────────────────────────────────────────────────────
Single source-of-truth for converting stored relative paths → full URLs.

Storage strategy:
  - Azure mode  → stores relative path:  products/folder/file.jpg
  - Local mode  → stores local path:     /uploads/products/folder/file.jpg

This module resolves either format to a full URL at read time using env vars.

Usage (in any repository or router):
    from app.shared.url_helpers import resolve_url

    full_url = resolve_url("products/folder/file.jpg")
    # → https://twamlivestorage2026.blob.core.windows.net/products/folder/file.jpg

    full_url = resolve_url("/uploads/products/folder/file.jpg")
    # → http://127.0.0.1:8000/uploads/products/folder/file.jpg  (local dev)
"""

import os
from urllib.parse import urlparse


def _account_name_from_connection_string(connection_string: str) -> str | None:
    for part in connection_string.split(";"):
        key, _, value = part.partition("=")
        if key.strip().lower() == "accountname" and value.strip():
            return value.strip()
    return None

# ── Read once at startup ──────────────────────────────────────────────────────
_CONN_STRING   = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")
_AZURE_ACCOUNT = (
    os.getenv("AZURE_STORAGE_ACCOUNT_NAME")
    or _account_name_from_connection_string(_CONN_STRING)
    or "twamstorage"
)
_AZURE_BASE    = f"https://{_AZURE_ACCOUNT}.blob.core.windows.net"

# BASE_URL is the backend's own public URL — used only for local /uploads/ paths
_BACKEND_URL   = os.getenv("BASE_URL", "http://127.0.0.1:8000").rstrip("/")

def resolve_url(path: str | None) -> str | None:
    """
    Convert any stored path format to a full public URL.

    Handles all 3 cases:
      1. Already a full URL       → return as-is
      2. Local /uploads/... path  → prepend BACKEND_URL
      3. Relative blob path       → prepend Azure base URL
    """
    if not path:
        return None

    # Already a full URL — return as-is (handles old data with full URLs stored)
    if path.startswith("http://") or path.startswith("https://"):
        return path

    # Local fallback path like /uploads/products/folder/file.jpg
    if path.startswith("/uploads/"):
        return f"{_BACKEND_URL}{path}"

    # Relative blob path like products/folder/file.jpg
    # or category/id/file.jpg, brands/file.jpg etc.
    return f"{_AZURE_BASE}/{path}"


def resolve_urls(paths: list[str | None]) -> list[str | None]:
    """Batch version of resolve_url for lists of paths."""
    return [resolve_url(p) for p in paths]


def storage_path_for_db(path: str | None) -> str | None:
    """
    Convert a display URL back to the compact path stored in the DB.

    Azure rows should store: products/folder/file.jpg
    Local fallback rows should store: /uploads/products/folder/file.jpg
    """
    if not path:
        return None

    value = str(path).strip().replace("\\", "/")
    if not value:
        return None

    if value.startswith("data:") or value.startswith("blob:"):
        return None

    if value.startswith("http://") or value.startswith("https://"):
        try:
            parsed_path = urlparse(value).path
        except Exception:
            parsed_path = value.split("/", 3)[-1] if "/" in value else value
        value = parsed_path

    clean = value.strip()
    if clean.startswith("/uploads/"):
        return clean

    clean = clean.lstrip("/")
    if clean.startswith("uploads/"):
        return f"/{clean}"

    if clean.startswith("product-images/"):
        return clean[len("product-images/"):]

    return clean
