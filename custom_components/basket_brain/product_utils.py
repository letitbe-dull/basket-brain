"""Size normalisation and brand extraction for cross-chain product comparison."""

from __future__ import annotations

import re
import string
from typing import Any

from rapidfuzz.fuzz import ratio

# ---------------------------------------------------------------------------
# Size normalisation
# ---------------------------------------------------------------------------

# Matches a size token anywhere in a string: 1L, 1.5l, 750ml, 500g, 2kg, 6pk,
# 6pack, 6-pack.  Units are case-insensitive.  The number group is captured
# so we can convert to a base unit.
_SIZE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(ml|l|kg|g|pk|pack)\b",
    re.IGNORECASE,
)

# "each" / "ea" standing alone (no leading number).
_EACH_RE = re.compile(r"\beach\b|\bea\b", re.IGNORECASE)

# Woolworths selling rules on loose produce ("Min Order 250g"), not pack sizes.
_MIN_ORDER_RE = re.compile(r"\bmin(?:imum)?\.?\s+order\s+\d+(?:\.\d+)?\s*(?:g|kg)\b", re.IGNORECASE)


def parse_size(text: str) -> tuple[float, str] | None:
    """Parse a size token from *text*, returning a (value, base_unit) pair.

    Base units: ``"ml"`` for volume, ``"g"`` for weight, ``"each"`` for count.
    Returns ``None`` when no parseable size is found.

    Examples::

        parse_size("Anchor Blue Milk 1L")      → (1000.0, "ml")
        parse_size("Pams White Sugar 1.5kg")   → (1500.0, "g")
        parse_size("Wattie's Peas 500g")       → (500.0, "g")
        parse_size("Free-Range Eggs 6pk")      → (6.0, "each")
        parse_size("Single Avocado each")      → (1.0, "each")
    """
    text = _MIN_ORDER_RE.sub(" ", text)
    for m in _SIZE_RE.finditer(text):
        value = float(m.group(1))
        unit = m.group(2).lower()
        if unit == "l":
            return (round(value * 1000, 6), "ml")
        if unit == "ml":
            return (value, "ml")
        if unit == "kg":
            return (round(value * 1000, 6), "g")
        if unit == "g":
            return (value, "g")
        if unit in ("pk", "pack"):
            return (value, "each")

    if _EACH_RE.search(text):
        return (1.0, "each")

    return None


def product_size(product: dict[str, Any]) -> tuple[float, str] | None:
    """Extract a normalised size from a product dict.

    Tries explicit size fields first (Woolworths ``Unit``/``PackageType``,
    Foodstuffs ``unit``), then falls back to parsing the product name.
    Returns ``None`` when no size can be determined.
    """
    for field in ("Unit", "PackageType", "unit", "size"):
        val = product.get(field)
        if val and isinstance(val, str):
            result = parse_size(val)
            if result is not None:
                return result

    name = product.get("name") or product.get("displayName") or ""
    if name:
        return parse_size(name)

    return None


# ---------------------------------------------------------------------------
# Brand extraction
# ---------------------------------------------------------------------------

_STRIP_PUNCT = str.maketrans("", "", string.punctuation)


def normalise_brand(brand: str) -> str:
    """Lowercase, trim, and strip punctuation from a brand string.

    Used for comparison only — does not alter display values.
    """
    return " ".join(brand.lower().strip().translate(_STRIP_PUNCT).split())


def extract_brand(product: dict[str, Any]) -> str | None:
    """Return the normalised brand for a product, suitable for comparison.

    Tries explicit brand fields (Woolworths and Foodstuffs both use ``brand``).
    Falls back to the leading word of the product name when no explicit field
    exists.  Returns ``None`` when neither source yields a value.
    """
    brand = product.get("brand") or product.get("Brand") or product.get("brandName")
    if brand and isinstance(brand, str) and brand.strip():
        return normalise_brand(brand)

    # Fallback: leading word of the name as a best-effort brand guess.
    name = product.get("name") or product.get("displayName") or ""
    words = name.strip().split()
    if words:
        return normalise_brand(words[0])

    return None


# ---------------------------------------------------------------------------
# Product identity + category
# ---------------------------------------------------------------------------


def _product_id(item: dict[str, Any]) -> str | None:
    """Pull a product ID out of a chain payload under any of its known keys."""
    pid = (
        item.get("sku")
        or item.get("id")
        or item.get("productId")
        or item.get("product_id")
    )
    return str(pid) if pid else None


def _category_l1(category_str: str) -> str:
    """Return the level-0 (department) portion of a category path string.

    "Bakery > Sliced Bread > White Bread" → "bakery".
    """
    return category_str.split(">")[0].strip().lower() if category_str else ""


def normalise_gtin(value: Any) -> str:
    """Canonicalise a GTIN/barcode for cross-chain comparison.

    Strips non-digits and leading zeros, so a zero-padded GTIN-14 and its
    GTIN-13 form compare equal — "09400566010085" and "9400566010085" both
    become "9400566010085". Chains store the same product's barcode with
    different padding, so a raw ``==`` misses real matches. Returns "" when
    there are no digits.
    """
    return re.sub(r"\D", "", str(value or "")).lstrip("0")


# ---------------------------------------------------------------------------
# Token / fuzzy phrase matching
# ---------------------------------------------------------------------------

# Packaging / unit words that say how a product is *sold*, never what it *is*.
_NOISE_TOKENS = frozenset({
    "bottle", "bottles", "can", "cans", "pack", "packs", "pk", "punnet",
    "bag", "bags", "box", "boxes", "jar", "jars", "tub", "tubs", "pouch",
    "each", "ea", "loaf", "block", "tray", "packet", "packets", "multipack",
})

_TOKEN_RE = re.compile(r"[a-z0-9]+")
# A token that is purely a size or quantity: 4, 90g, 2l, 1.5kg, 6pk, 750ml, 2x.
_SIZE_TOKEN_RE = re.compile(r"^(?:x?\d+(?:\.\d+)?(?:g|kg|ml|l|pk|pack)?|\d+x)$")

# A typed word must match a product word at least this well to count as present.
_WORD_MATCH_MIN = 82.0


def meaningful_tokens(text: str) -> list[str]:
    """Break *text* into the words that say what a product IS.

    Lowercases, strips possessive apostrophes so "Vogel's" stays one word,
    splits on non-alphanumerics, then drops packaging words and pure
    size/quantity tokens. "4 Bananas" → ["bananas"]; "Doritos Corn Chips
    Supreme Cheese 90g" → ["doritos", "corn", "chips", "supreme", "cheese"].
    """
    cleaned = text.lower().replace("'", "").replace("’", "")
    return [
        w
        for w in _TOKEN_RE.findall(cleaned)
        if w not in _NOISE_TOKENS and not _SIZE_TOKEN_RE.match(w)
    ]


# Words that make a product a different *kind* of the same thing. UHT milk is
# not fresh milk, even at the same brand and size. Each marker maps to a
# canonical variant so wordings ("long life", "longlife") compare equal.
_VARIANT_MARKERS = {
    "uht": "uht",
    "long life": "uht",
    "longlife": "uht",
    "shelf stable": "uht",
    "powder": "powder",
    "powdered": "powder",
    "concentrate": "concentrate",
    # Plant bases. Dairy carries no marker, so an unmarked anchor can never
    # match a marked candidate — which is what stops a 1L dairy milk landing
    # on a cheaper 1L soy at a chain where the brand doesn't match.
    "soy": "soy",
    "soya": "soy",
    "oat": "oat",
    "almond": "almond",
    "cashew": "cashew",
    "macadamia": "macadamia",
    "rice": "rice",
    "coconut": "coconut",
    "lactose free": "lactose_free",
}


# Milk fat levels, in NZ terms. These can't live in _VARIANT_MARKERS: absence
# of a fat word doesn't mean "no fat level", it means standard — chains label
# the same product "Milk 1L" or "Standard Milk 1L" interchangeably. So the
# default has to be filled in, and only for milk.
_MILK_FAT_MARKERS = {
    "trim": "trim",
    "green top": "trim",
    "skim": "trim",
    "skimmed": "trim",
    "fat free": "trim",
    "non fat": "trim",
    "lite": "lite",
    "light blue": "lite",
    "low fat": "lite",
    "standard": "standard",
    "blue top": "standard",
    "homogenised": "standard",
    "homogenized": "standard",
    "full cream": "standard",
    "whole": "standard",
    "creamy": "creamy",
    "gold top": "creamy",
    "silver top": "creamy",
    "jersey": "creamy",
}

_MILK_RE = re.compile(r"\bmilks?\b")

# "Milk" in the name without being milk you drink. These have their own sizes
# and fat rules, so leave them out of the fat gate entirely.
_NOT_DRINKING_MILK = ("chocolate", "milkshake", "condensed", "evaporated")


def _milk_fat_markers(cleaned: str) -> frozenset[str]:
    """Fat level for drinking milk, defaulting to standard when unlabelled."""
    if not _MILK_RE.search(cleaned):
        return frozenset()
    if any(word in cleaned for word in _NOT_DRINKING_MILK):
        return frozenset()
    found = frozenset(
        canon
        for marker, canon in _MILK_FAT_MARKERS.items()
        if re.search(rf"\b{re.escape(marker)}\b", cleaned)
    )
    return found or frozenset({"standard"})


def variant_markers(text: str) -> frozenset[str]:
    """Return the canonical variant markers present in *text*.

    Used to keep cross-chain matching honest: a candidate must carry the same
    variants as the anchor, so a 1L UHT never stands in for a 1L fresh.
    """
    cleaned = " ".join(text.lower().replace("-", " ").split())
    markers = {
        canon
        for marker, canon in _VARIANT_MARKERS.items()
        if re.search(rf"\b{re.escape(marker)}\b", cleaned)
    }
    return frozenset(markers | _milk_fat_markers(cleaned))


def name_satisfies(phrase: str, name: str) -> float | None:
    """Score how well *name* satisfies every meaningful word of *phrase*.

    For each typed word we take its best fuzzy match among the product's words,
    so plurals and typos still land ("bananas" ↔ "banana"). Returns the mean
    best-match ratio (0–100) when *every* typed word is represented; returns
    None when any word is missing — the hard gate that stops "milk" resolving
    to something with no milk in its name at all.

    Ranking on the returned score naturally prefers exact words over fuzzy
    ones ("bananas" scores 100 against "…bananas…" but ~92 against "…banana
    lifesavers"), so the real product beats the look-alike.
    """
    p = meaningful_tokens(phrase)
    n = meaningful_tokens(name)
    if not p or not n:
        return None
    total = 0.0
    for pt in p:
        best = max(ratio(pt, nt) for nt in n)
        if best < _WORD_MATCH_MIN:
            return None
        total += best
    return total / len(p)
