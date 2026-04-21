"""
speech/repository.py
─────────────────────
Two responsibilities:
  1. Load Azure Speech credentials from the speech.azure_speech_config table.
  2. Build phrase hint list from live DB (brands, categories, products, colors)
     combined with static lingerie vocabulary extracted from the product catalogue.

Credentials are cached in-process for 10 minutes so we do not hit the DB on
every voice request.
"""

import time
from typing import Optional
from sqlalchemy import text
from database import engine


# ── Credential cache ──────────────────────────────────────────────────────────
_CRED_CACHE: Optional[dict] = None
_CRED_CACHE_TS: float = 0.0
_CRED_TTL: float = 600.0          # refresh credentials every 10 minutes


def get_speech_credentials() -> dict:
    """
    Return {"subscription_key": ..., "region": ...} from the DB.
    Raises RuntimeError if no active config row exists.
    """
    global _CRED_CACHE, _CRED_CACHE_TS

    now = time.monotonic()
    if _CRED_CACHE and (now - _CRED_CACHE_TS) < _CRED_TTL:
        return _CRED_CACHE

    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT subscription_key, region
            FROM   speech.azure_speech_config
            WHERE  is_active = TRUE
            ORDER  BY id DESC
            LIMIT  1
        """)).fetchone()

    if not row:
        raise RuntimeError(
            "No active Azure Speech config found in speech.azure_speech_config"
        )

    _CRED_CACHE    = {"subscription_key": row[0], "region": row[1]}
    _CRED_CACHE_TS = now
    return _CRED_CACHE


# ── Phrase hints ──────────────────────────────────────────────────────────────
# Azure PhraseListGrammar supports up to ~500 phrases.
# We pull the most important ones (brand / category / product / color) from the
# live DB so new products are automatically recognised without redeployment.
# We also include a hardcoded static list extracted from the full catalogue CSV
# so the recogniser works even before any new data is added to the DB.

_STATIC_PHRASES: list[str] = [
    # ── Brands (from Brand.csv) ──────────────────────────────────────────────
    "TWAM", "Amante", "Blossoms", "Enamor", "Soie", "Triumph",

    # ── Categories (from Category.csv) ──────────────────────────────────────
    "Bras", "Panties", "Active Wear", "Balconette Bras", "Bandeau Bras",
    "Beginners Bras", "Bikini", "Boy Shorts", "Bralette bra", "Bridal Bras",
    "Brief", "Cotton Bras", "Cycling shorts", "Essentials", "Full Cup Bras",
    "High-Waist and High-Leg", "Hipster", "Lace", "Leakproof",
    "Mastectomy Bras", "Minimizer Bras", "Non-padded and Non-Wired bra",
    "Nursing Bras", "Push up Bras", "Seamless Bras", "Shapewear",
    "Sports bra", "Strapless Bras", "Swim Wear", "T-Shirt Bras",
    "Thongs and V-Strings",

    # ── Product names (from Products.csv) ────────────────────────────────────
    "Comfort Non-Wired Bra", "Cotton Comfort Bra", "Everyday Non-Wired Bra",
    "Front-Open Zip Bra", "Full Coverage Everyday Bra",
    "Full Coverage Seamless Bra", "Full Cup Minimizer Bra",
    "Full Support Minimizer Bra", "High Support Sports Bra",
    "High-Waist Full Panty", "Lace Hipster Panty", "Long Cycling Shorts",
    "Medium Support Sports Bra", "Printed Bikini Brief", "Seamless Crop Bra",
    "Shaping Camisole", "Short Cycling Shorts", "Soft Non-Padded Bra",

    # ── Colors (from ProductColor.csv + ProductVariants.csv) ─────────────────
    "Aqua", "Ash Grey", "Bark", "Beige", "Black", "Blue", "Chocolate",
    "Cinnamon", "Cloud", "Coffee", "Coffee Cream", "Coral", "Coral Red",
    "Dark Skin", "Deep Blue", "Desert Rose", "Earth Red", "Fudge", "Grass",
    "Grey", "Lemon", "Maroon", "Midnight", "Mist", "Nude", "Purple",
    "Red", "Skin", "Teal", "Waffle", "White",

    # ── Product descriptors & fabric terms ───────────────────────────────────
    "non-wired", "wireless", "underwire", "padded", "non-padded",
    "moulded", "seamless", "push up", "balconette", "strapless",
    "front open", "racerback", "cross back", "lace", "cotton", "mesh",
    "microfiber", "modal", "spandex", "elastane", "breathable",
    "anti-chafe", "moisture wicking", "adjustable", "removable",
    "convertible", "double layer", "wire-free", "hook closure",
    "full coverage", "tummy control", "longline", "crop",

    # ── Size expressions ─────────────────────────────────────────────────────
    "extra small", "small", "medium", "large", "extra large",
    "double extra large", "free size",
    "thirty two", "thirty four", "thirty six", "thirty eight", "forty",
    "thirty two B", "thirty four C", "thirty six D",

    # ── Search / navigation phrases ──────────────────────────────────────────
    "show me", "find", "search for", "I want", "I need", "give me",
    "add to cart", "best seller", "new arrivals", "all products",
    "sale", "discount", "collection",
]


def build_phrase_hints() -> list[str]:
    """
    Return a deduplicated phrase list: live DB terms + static fallback.
    DB terms are fetched fresh on every call (typically called once per request
    — the SDK only needs the list at recogniser-creation time).
    """
    phrases: list[str] = list(_STATIC_PHRASES)   # start with static

    try:
        with engine.connect() as conn:
            # Brands
            rows = conn.execute(text(
                'SELECT "brandName" FROM public."Brand" '
                'WHERE ("deletedInd" IS NULL OR "deletedInd" = FALSE) '
                'AND "isActive" = TRUE'
            )).fetchall()
            phrases.extend(r[0] for r in rows if r[0])

            # Categories
            rows = conn.execute(text(
                'SELECT "Name" FROM public."Category" '
                'WHERE ("DeletedInd" IS NULL OR "DeletedInd" = FALSE)'
            )).fetchall()
            phrases.extend(r[0] for r in rows if r[0])

            # Product names
            rows = conn.execute(text(
                'SELECT "Name" FROM public."Products" '
                'WHERE ("DeletedInd" IS NULL OR "DeletedInd" = FALSE)'
            )).fetchall()
            phrases.extend(r[0] for r in rows if r[0])

            # Colors
            rows = conn.execute(text(
                'SELECT "ColorName" FROM public."ProductColor"'
            )).fetchall()
            phrases.extend(r[0] for r in rows if r[0])

    except Exception:
        # If DB query fails, static list is still used — no crash
        pass

    # Deduplicate preserving order, strip whitespace, drop blanks
    seen: set[str] = set()
    result: list[str] = []
    for p in phrases:
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            result.append(p)

    # Azure PhraseListGrammar cap: 500 phrases
    return result[:500]