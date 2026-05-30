"""
app/speech/repository.py
─────────────────────────
Two responsibilities:
  1. Load Azure Speech credentials from speech.azure_speech_config (10-min cache).
  2. Build phrase hint list with three layers of accuracy improvement:

     Layer 1 — Exact phrases from DB (live brands/categories/products/colors)
     Layer 2 — Static phrases repeated 3× for brands, 2× for categories/products
               to increase Azure acoustic model weight
     Layer 3 — Phonetic near-miss variants for every important term so Azure
               maps "tom" → TWAM, "soy" → Soie, "bro" → bra, etc.

Azure PhraseListGrammar cap is 500. We stay under that.
"""

import time
from typing import Optional
from sqlalchemy import text
from database import engine


# ── Credential cache ───────────────────────────────────────────────────────────
_CRED_CACHE: Optional[dict] = None
_CRED_CACHE_TS: float = 0.0
_CRED_TTL: float = 600.0


def get_speech_credentials() -> dict:
    """Return {"subscription_key": ..., "region": ...} from the DB."""
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


# ── Static phrase list ─────────────────────────────────────────────────────────
# Brands repeated 3× → maximum weight boost in Azure acoustic model
# Categories/products repeated 2× → strong boost
# Phonetic near-misses added for every short/unusual word
_STATIC_PHRASES: list[str] = [

    # ══════════════════════════════════════════════════════════════════════════
    # BRANDS  (3× each + phonetic near-misses)
    # ══════════════════════════════════════════════════════════════════════════

    # TWAM — mishears as: tom, tram, team, twee am, t wham
    "TWAM", "TWAM", "TWAM",
    "twam", "tee wam", "t wam", "t-wam", "twee am", "t wham",

    # Amante — mishears as: amanda, amount, a mante, armante
    "Amante", "Amante", "Amante",
    "amante", "a mante", "ah mante", "amantay", "armante",

    # Blossoms — mishears as: blossom, blossem, bloss ums
    "Blossoms", "Blossoms", "Blossoms",
    "blossom", "blossem", "bloss ums",

    # Enamor — mishears as: enamour, in amor, in a more, en amor
    "Enamor", "Enamor", "Enamor",
    "enamour", "in amor", "in a more", "en amor", "en amour",

    # Soie — mishears as: soy, soya, swah, swoi, swoy, so way
    "Soie", "Soie", "Soie",
    "soy", "soya", "swah", "swoi", "swoy", "so way",

    # Triumph — mishears as: try umph, tri umf, triumph
    "Triumph", "Triumph", "Triumph",
    "try umph", "tri umf", "tri umph",

    # ══════════════════════════════════════════════════════════════════════════
    # CATEGORIES  (2× each + phonetic near-misses)
    # ══════════════════════════════════════════════════════════════════════════

    # Bras — mishears as: bro, braw, brus, braz
    "Bras", "Bras",
    "bra", "braw", "bro", "brus", "braz",

    # Panties — mishears as: panty, pantie, pant ease, pan tees
    "Panties", "Panties",
    "panty", "pantie", "pant ease", "pan tees", "pantys",

    # Active Wear
    "Active Wear", "Active Wear",
    "activewear", "active where", "active ware",

    # Balconette Bras — mishears as: balcony bra, bal co net
    "Balconette Bras", "Balconette Bras",
    "balconette", "balcony bra", "bal co net", "bal co nette",

    # Bandeau Bras — mishears as: band oh, ban do, bando
    "Bandeau Bras", "Bandeau Bras",
    "bandeau", "band oh", "ban do", "bando bra",

    # Beginners Bras
    "Beginners Bras", "Beginners Bras",
    "beginner bra", "starter bra", "first bra",

    # Bikini — mishears as: bi kini, be keeny
    "Bikini", "Bikini",
    "bi kini", "bikinis", "be keeny",

    # Boy Shorts — mishears as: boy shot, boy short
    "Boy Shorts", "Boy Shorts",
    "boy short", "boyshorts", "boy shot", "boishorts",

    # Bralette bra — mishears as: bra let, bra lette
    "Bralette bra", "Bralette bra",
    "bralette", "bra let", "bra lette", "bra-lette",

    # Bridal Bras
    "Bridal Bras", "Bridal Bras",
    "bridal bra", "wedding bra", "bride bra",

    # Brief — mishears as: breef, brif
    "Brief", "Brief",
    "briefs", "brief panty", "breef", "brif",

    # Cotton Bras
    "Cotton Bras", "Cotton Bras",
    "cotton bra", "all cotton bra",

    # Cycling shorts — mishears as: cycle shorts, cycling shot
    "Cycling shorts", "Cycling shorts",
    "cycling short", "cycle shorts", "bike shorts", "cycling shot",

    # Essentials
    "Essentials", "Essentials",
    "essential", "basics", "everyday essentials",

    # Full Cup Bras
    "Full Cup Bras", "Full Cup Bras",
    "full cup bra", "full cup",

    # High-Waist and High-Leg
    "High-Waist and High-Leg", "High-Waist and High-Leg",
    "high waist", "high leg", "high waisted", "high waist panty",

    # Hipster — mishears as: hip star, hipstar
    "Hipster", "Hipster",
    "hipsters", "hip star", "hipstar",

    # Lace
    "Lace", "Lace",
    "lacy", "lace panty", "lace bra", "lace design",

    # Leakproof — mishears as: leak proof
    "Leakproof", "Leakproof",
    "leak proof", "leak-proof", "period panty", "period underwear",

    # Mastectomy Bras
    "Mastectomy Bras", "Mastectomy Bras",
    "mastectomy bra", "post surgery bra", "post mastectomy",

    # Minimizer Bras — mishears as: minimiser, minimize
    "Minimizer Bras", "Minimizer Bras",
    "minimizer bra", "minimiser bra", "minimizer", "minimiser",

    # Non-padded and Non-Wired bra
    "Non-padded and Non-Wired bra", "Non-padded and Non-Wired bra",
    "non padded", "non wired", "wire free bra", "no wire bra",

    # Nursing Bras
    "Nursing Bras", "Nursing Bras",
    "nursing bra", "feeding bra", "maternity bra", "breastfeeding bra",

    # Push up Bras — mishears as: push up, push-up
    "Push up Bras", "Push up Bras",
    "push up bra", "pushup bra", "push-up bra", "push up",

    # Seamless Bras
    "Seamless Bras", "Seamless Bras",
    "seamless bra", "no seam bra",

    # Shapewear — mishears as: shape ware, shape where
    "Shapewear", "Shapewear",
    "shape wear", "shaper", "tummy control", "shape where",

    # Sports bra — mishears as: sport bra, gym bra
    "Sports bra", "Sports bra",
    "sport bra", "gym bra", "workout bra", "exercise bra",

    # Strapless Bras — mishears as: strap less
    "Strapless Bras", "Strapless Bras",
    "strapless bra", "strap less bra", "no strap bra",

    # Swim Wear
    "Swim Wear", "Swim Wear",
    "swimwear", "swim suit", "swimming wear", "swimsuit",

    # T-Shirt Bras — mishears as: tee shirt bra, tshirt bra
    "T-Shirt Bras", "T-Shirt Bras",
    "t shirt bra", "tee shirt bra", "tshirt bra",

    # Thongs and V-Strings — mishears as: v string, g string
    "Thongs and V-Strings", "Thongs and V-Strings",
    "thong", "thongs", "v string", "g string", "v-string",

    # ══════════════════════════════════════════════════════════════════════════
    # PRODUCT NAMES  (2× each + common short versions)
    # ══════════════════════════════════════════════════════════════════════════

    "Comfort Non-Wired Bra", "Comfort Non-Wired Bra",
    "comfort bra", "non wired comfort bra",

    "Cotton Comfort Bra", "Cotton Comfort Bra",
    "cotton comfort",

    "Everyday Non-Wired Bra", "Everyday Non-Wired Bra",
    "everyday bra", "daily bra", "everyday non wired",

    "Front-Open Zip Bra", "Front-Open Zip Bra",
    "front open bra", "zip bra", "front zip bra",

    "Full Coverage Everyday Bra", "Full Coverage Everyday Bra",
    "full coverage bra", "full cover bra",

    "Full Coverage Seamless Bra", "Full Coverage Seamless Bra",
    "seamless full coverage bra",

    "Full Cup Minimizer Bra", "Full Cup Minimizer Bra",
    "full cup minimizer",

    "Full Support Minimizer Bra", "Full Support Minimizer Bra",
    "full support bra", "support minimizer",

    "High Support Sports Bra", "High Support Sports Bra",
    "high support bra", "running bra", "high impact bra",

    "High-Waist Full Panty", "High-Waist Full Panty",
    "high waist full panty", "full panty",

    "Lace Hipster Panty", "Lace Hipster Panty",
    "lace hipster",

    "Long Cycling Shorts", "Long Cycling Shorts",
    "long cycle shorts", "long bike shorts",

    "Medium Support Sports Bra", "Medium Support Sports Bra",
    "medium support bra", "yoga bra", "pilates bra",

    "Printed Bikini Brief", "Printed Bikini Brief",
    "printed bikini", "bikini brief", "print bikini",

    "Seamless Crop Bra", "Seamless Crop Bra",
    "crop bra", "seamless crop",

    "Shaping Camisole", "Shaping Camisole",
    "camisole", "cam i sole", "shaping top", "cami",

    "Short Cycling Shorts", "Short Cycling Shorts",
    "short cycle shorts", "short bike shorts",

    "Soft Non-Padded Bra", "Soft Non-Padded Bra",
    "soft bra", "non padded soft bra",

    # ══════════════════════════════════════════════════════════════════════════
    # COLORS  (spoken color names and phonetic variants)
    # ══════════════════════════════════════════════════════════════════════════

    "Aqua", "aqua blue", "aqua color",
    "Ash Grey", "ash gray", "ash grey color",
    "Bark", "bark brown", "bark color",
    "Beige", "bej", "beige color", "bei ge",
    "Black", "jet black", "black color",
    "Blue", "navy blue", "blue color",
    "Chocolate", "chocolate brown", "choco",
    "Cinnamon", "cinna mon", "cinnamon brown",
    "Cloud", "cloud white", "cloud color",
    "Coffee", "coffee brown", "coffee color",
    "Coffee Cream", "coffee and cream", "coffee cream",
    "Coral", "coral pink", "coral color",
    "Coral Red", "coral red color",
    "Dark Skin", "dark skin tone", "dark skin color",
    "Deep Blue", "deep blue color",
    "Desert Rose", "desert rose pink", "desert rose color",
    "Earth Red", "earth red color",
    "Fudge", "fudge brown", "fudge color",
    "Grass", "grass green", "grass color",
    "Grey", "gray", "grey color", "gray color",
    "Lemon", "lemon yellow", "lemon color",
    "Maroon", "maroon red", "dark maroon",
    "Midnight", "midnight blue", "midnight black",
    "Mist", "misty", "mist color",
    "Nude", "nude color", "skin color nude",
    "Purple", "violet", "purple color",
    "Red", "bright red", "red color",
    "Skin", "skin tone", "skin color",
    "Teal", "teal blue", "teal green",
    "Waffle", "waffle color",
    "White", "pure white", "white color",

    # ══════════════════════════════════════════════════════════════════════════
    # FABRIC & PRODUCT DESCRIPTORS
    # ══════════════════════════════════════════════════════════════════════════
    "non-wired", "non wired", "wireless", "wire free", "no wire",
    "underwire", "under wire",
    "padded", "non-padded", "non padded", "unpadded",
    "moulded", "molded", "moulded cup",
    "seamless", "no seam",
    "push up", "push-up",
    "balconette", "balcony",
    "strapless", "strap less",
    "front open", "front-open", "zip front",
    "racerback", "racer back",
    "cross back",
    "lace", "lacy",
    "cotton", "all cotton",
    "mesh", "mesh fabric",
    "microfiber", "micro fiber",
    "modal", "modal fabric",
    "spandex", "span dex",
    "elastane", "elas tane",
    "breathable",
    "anti-chafe", "anti chafe",
    "moisture wicking", "moisture-wicking",
    "adjustable",
    "removable",
    "convertible",
    "double layer",
    "hook closure",
    "full coverage", "full cover",
    "tummy control",
    "longline", "long line",
    "crop", "cropped",
    "minimizer", "minimiser",

    # ══════════════════════════════════════════════════════════════════════════
    # SIZES  (how users actually say them in Indian English)
    # ══════════════════════════════════════════════════════════════════════════
    "extra small", "XS",
    "small", "S size",
    "medium", "M size",
    "large", "L size",
    "extra large", "XL",
    "double extra large", "XXL",
    "free size",
    "thirty two", "thirty two A", "thirty two B", "thirty two C", "thirty two D",
    "thirty four", "thirty four A", "thirty four B", "thirty four C", "thirty four D",
    "thirty six", "thirty six A", "thirty six B", "thirty six C", "thirty six D",
    "thirty eight", "thirty eight A", "thirty eight B", "thirty eight C", "thirty eight D",
    "forty", "forty A", "forty B", "forty C", "forty D",

    # ══════════════════════════════════════════════════════════════════════════
    # SEARCH / NAVIGATION PHRASES users actually say
    # ══════════════════════════════════════════════════════════════════════════
    "show me", "show me all",
    "find", "find me",
    "search for", "search",
    "I want", "I need", "I am looking for",
    "give me", "get me",
    "add to cart",
    "best seller", "best sellers", "bestseller",
    "new arrivals", "new arrival", "new",
    "all products", "everything",
    "sale", "on sale", "discount", "offer",
    "collection",
    "under five hundred", "under thousand", "under five hundred rupees",
]


def build_phrase_hints() -> list[str]:
    """
    Return phrase list: static (with boosts + phonetic variants) + live DB terms.
    DB terms are added so new products are picked up without redeployment.
    Deduplication preserves order (first occurrence wins — static list priority).
    Capped at 500 per Azure limit.
    """
    phrases: list[str] = list(_STATIC_PHRASES)

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
                'SELECT DISTINCT "Color" FROM twam."ProductVariants" WHERE "Color" IS NOT NULL AND ("DeletedInd" = false OR "DeletedInd" IS NULL)'
            )).fetchall()
            phrases.extend(r[0] for r in rows if r[0])

    except Exception:
        pass  # static list is still used — no crash

    # Deduplicate preserving order, strip whitespace, drop blanks
    seen: set[str] = set()
    result: list[str] = []
    for p in phrases:
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            result.append(p)

    return result[:500]
