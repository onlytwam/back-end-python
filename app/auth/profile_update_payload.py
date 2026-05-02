"""
Compatibility shim.

Profile update payload parsing now lives in app.auth.repository alongside the
profile update save logic. This module only re-exports the parser so any stale
imports continue to work until the file can be removed completely.
"""

from .repository import parse_profile_update_payload

__all__ = ["parse_profile_update_payload"]
