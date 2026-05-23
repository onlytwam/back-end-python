"""
color_init.py
Auto-creates and populates twam."ProductColor" table at startup.
pv."Color" stores COLOUR NAMES (e.g. "Dark Navy Blue", "Nude", "Wine"),
so we:
  ColorName = human-readable colour name (title-cased)
  HexValue  = resolved hex (used for proximity search)

If an old product stored a raw hex like "#1a1a1a" in pv.Color,
we reverse-map it to the nearest known colour name so the table
stays human-readable throughout.
"""
from sqlalchemy.orm import Session
from sqlalchemy import text


def _hex_to_human_name(hex_val: str) -> str:
    """
    Convert a hex string like '#1a1a1a' to a human name like 'Black'.
    Uses the same COLOR_NAME_TO_HEX map from color_utils (nearest match).
    """
    from app.shared.color_utils import COLOR_NAME_TO_HEX, _hex_to_rgb, _redmean_distance
    import math

    try:
        r, g, b = _hex_to_rgb(hex_val)
    except Exception:
        return hex_val  # can't parse — keep as-is

    best_name = hex_val
    best_dist = float("inf")
    for name, candidate_hex in COLOR_NAME_TO_HEX.items():
        try:
            cr, cg, cb = _hex_to_rgb(candidate_hex)
            rm = (r + cr) / 2
            dr, dg, db = r - cr, g - cg, b - cb
            dist = math.sqrt(
                (2 + rm / 256) * dr * dr + 4 * dg * dg + (2 + (255 - rm) / 256) * db * db
            )
            if dist < best_dist:
                best_dist = dist
                best_name = name
        except Exception:
            pass

    # Title-case: "dark navy blue" → "Dark Navy Blue"
    return best_name.title()


def init_product_color_table(db: Session) -> None:
    # Step 1: Create table
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS twam."ProductColor" (
            "ColorId"     SERIAL PRIMARY KEY,
            "ColorName"   TEXT NOT NULL UNIQUE,
            "HexValue"    TEXT NOT NULL,
            "CreatedDate" TIMESTAMP DEFAULT NOW()
        )
    """))
    db.commit()

    # Step 2: Get all distinct pv.Color values
    rows = db.execute(text("""
        SELECT DISTINCT "Color"
        FROM twam."ProductVariants"
        WHERE "Color" IS NOT NULL
          AND "Color" != ''
          AND ("DeletedInd" = false OR "DeletedInd" IS NULL)
    """)).fetchall()

    # Step 3: Upsert — ColorName = human name, HexValue = resolved hex
    from app.shared.color_utils import resolve_hex

    for (color_val,) in rows:
        if not color_val:
            continue

        stripped = color_val.strip()
        stripped_lower = stripped.lower()

        # Determine the hex value
        if stripped_lower.startswith('#') and len(stripped_lower) in (4, 7):
            # pv.Color contains a raw hex — resolve to human name + keep hex
            hex_val = stripped_lower
            human_name = _hex_to_human_name(hex_val)
        else:
            # pv.Color contains a colour name — resolve to hex
            human_name = stripped  # keep original casing (e.g. "Dark Navy Blue")
            hex_val = resolve_hex(stripped_lower) or '#808080'

        db.execute(text("""
            INSERT INTO twam."ProductColor" ("ColorName", "HexValue")
            VALUES (:name, :hex)
            ON CONFLICT ("ColorName")
            DO UPDATE SET "HexValue" = EXCLUDED."HexValue"
        """), {"name": human_name, "hex": hex_val})

    db.commit()
