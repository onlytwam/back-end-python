"""
color_utils.py
Simplified — hex removed completely.

ColorFamily is stored directly on twam."ProductVariants"."ColorFamily" (admin-set).
Filtering and search use that field via direct equality / ILIKE.
No proxy table, no hex lookups.
"""
from typing import Optional


# ── Color name → family ────────────────────────────────────────────────────────
# Used server-side to:
#   • back-fill ColorFamily when admin hasn't set it yet
#   • search: map a query like "navy" to family "Blue", then filter by ColorFamily
COLOR_FAMILY_MAP: dict[str, str] = {
    # Black
    "black": "Black", "dark black": "Black", "jet black": "Black",
    "onyx": "Black", "ebony": "Black", "charcoal": "Black", "graphite": "Black",
    # White
    "white": "White", "ivory": "White", "cream": "White", "snow": "White",
    "pearl": "White", "off-white": "White", "off white": "White", "cloud": "White",
    # Grey
    "grey": "Grey", "gray": "Grey", "silver": "Grey", "slate": "Grey",
    "ash": "Grey", "smoke": "Grey", "mist": "Grey",
    # Red (dark reds first so exact match wins)
    "wine": "Red", "burgundy": "Red", "maroon": "Red", "oxblood": "Red",
    "bordeaux": "Red", "claret": "Red", "sangria": "Red", "dark red": "Red",
    "dark wine": "Red", "earth red": "Red",
    "red": "Red", "crimson": "Red", "scarlet": "Red", "cherry": "Red",
    "ruby": "Red", "coral red": "Red", "brick": "Red",
    # Pink
    "pink": "Pink", "light pink": "Pink", "hot pink": "Pink", "baby pink": "Pink",
    "blush": "Pink", "rose": "Pink", "flamingo": "Pink", "magenta": "Pink",
    "fuchsia": "Pink", "desert rose": "Pink", "dusty rose": "Pink",
    "dusty pink": "Pink", "desert_rose": "Pink", "deep pink": "Pink",
    # Purple
    "purple": "Purple", "violet": "Purple", "lavender": "Purple",
    "mauve": "Purple", "lilac": "Purple", "plum": "Purple", "indigo": "Purple",
    "grape": "Purple", "amethyst": "Purple", "orchid": "Purple",
    # Blue (teal/aqua included per filter definition)
    "blue": "Blue", "navy": "Blue", "navy blue": "Blue", "dark blue": "Blue",
    "midnight blue": "Blue", "cobalt": "Blue", "sapphire": "Blue",
    "denim": "Blue", "teal": "Blue", "dark teal": "Blue", "turquoise": "Blue",
    "aqua": "Blue", "jade": "Blue", "sea green": "Blue", "deep blue": "Blue",
    "midnight": "Blue",
    # Green
    "green": "Green", "olive": "Green", "mint": "Green", "sage": "Green",
    "forest": "Green", "lime": "Green", "grass": "Green",
    # Yellow
    "yellow": "Yellow", "gold": "Yellow", "mustard": "Yellow",
    "amber": "Yellow", "lemon": "Yellow",
    # Orange
    "orange": "Orange", "rust": "Orange", "burnt orange": "Orange",
    "terracotta": "Orange", "tangerine": "Orange", "coral": "Orange",
    "apricot": "Orange",
    # Brown / Nude / Skin
    "brown": "Brown", "tan": "Brown", "beige": "Brown", "caramel": "Brown",
    "chocolate": "Brown", "mocha": "Brown", "nude": "Brown", "skin": "Brown",
    "sand": "Brown", "peach": "Brown", "champagne": "Brown", "wheat": "Brown",
    "bark": "Brown", "fudge": "Brown", "waffle": "Brown", "coffee": "Brown",
    "coffee cream": "Brown", "cinnamon": "Brown", "dark skin": "Brown",
    "taupe": "Brown", "rose gold": "Brown",
}


def get_color_family(color_name: str) -> Optional[str]:
    """Return the family label for a given color name, or None."""
    return COLOR_FAMILY_MAP.get(color_name.lower().strip())