"""
color_init.py — STUB

twam."ProductColor" table has been dropped.
ColorFamily is stored directly on twam."ProductVariants"."ColorFamily".

This file is kept so startup imports don't crash. The function is a no-op.
"""
from sqlalchemy.orm import Session


def init_product_color_table(db: Session) -> None:
    """No-op — ProductColor table removed."""
    pass