"""L0 — Query normalizer (~0.5ms, no I/O).

Cleans and standardises the raw user query before cache lookup or search:
  • lowercase + strip whitespace
  • remove punctuation noise
  • expand common e-commerce synonyms (tshirt → t-shirt)
  • detect language code
  • extract price filters if embedded in query ("under 500", "below ₹1000")
  • extract stock hint ("in stock", "available")
  • return a NormalizedQuery dataclass consumed by every downstream layer
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional


# ── Synonym map ───────────────────────────────────────────────────────────────
# Expands informal or abbreviated terms to canonical product vocabulary.
_SYNONYMS: dict[str, str] = {
    "tshirt": "t-shirt",
    "t shirt": "t-shirt",
    "tee": "t-shirt",
    "jeans": "denim jeans",
    "trousers": "pants",
    "footwear": "shoes",
    "sneakers": "shoes",
    "trainers": "shoes",
    "specs": "glasses",
    "eyewear": "glasses",
    "laptop bag": "laptop bag",
    "mobile": "phone",
    "cell phone": "phone",
    "smartphone": "phone",
    "earphones": "earbuds",
    "headphones": "headphones",
    "kurta": "kurta",
    "salwar": "salwar kameez",
    "saree": "saree",
    "dupatta": "dupatta",
    "kurti": "kurti",
    "lehenga": "lehenga",
    "sherwani": "sherwani",
}

# ── Price extraction patterns ─────────────────────────────────────────────────
# Matches: "under 500", "below ₹1000", "less than 2000", "upto 300"
_PRICE_UNDER_RE = re.compile(
    r"(?:under|below|less\s+than|upto|up\s+to|max(?:imum)?)\s*[₹$€£]?\s*(\d[\d,]*)",
    re.IGNORECASE,
)
# Matches: "above 500", "over ₹200", "more than 1000", "min 400", "starting from 300"
_PRICE_OVER_RE = re.compile(
    r"(?:above|over|more\s+than|min(?:imum)?|starting\s+from|from)\s*[₹$€£]?\s*(\d[\d,]*)",
    re.IGNORECASE,
)
# Matches: "between 200 and 500", "200-500", "₹200 to ₹500"
_PRICE_RANGE_RE = re.compile(
    r"[₹$€£]?\s*(\d[\d,]*)\s*(?:to|-|and)\s*[₹$€£]?\s*(\d[\d,]*)",
    re.IGNORECASE,
)

# ── Stock hints ───────────────────────────────────────────────────────────────
_IN_STOCK_RE = re.compile(
    r"\b(?:in\s+stock|available|in\s+store|available\s+now)\b",
    re.IGNORECASE,
)

# ── Discriminating-attribute detector ─────────────────────────────────────────
# Queries that pin a specific colour / size / capacity / model must NOT be served
# from the L2 semantic cache: "red shoes" and "blue shoes" embed >0.92 similar, so
# a fuzzy hit would return the wrong variant. When any of these match, callers skip
# L2 and go straight to L3 (exact L1 cache is still safe — it keys on the full text).
_COLOR_WORDS = (
    "black", "white", "red", "blue", "green", "yellow", "orange", "purple",
    "pink", "brown", "grey", "gray", "beige", "gold", "silver", "navy",
    "maroon", "teal", "cream", "tan", "olive", "burgundy",
)
_ATTRIBUTE_RE = re.compile(
    r"\b(?:" + "|".join(_COLOR_WORDS) + r")\b"          # colours
    r"|\b(?:xs|s|m|l|xl|xxl|xxxl|small|medium|large)\b"  # clothing sizes
    r"|\b(?:uk|us|eu)\s*\d{1,2}\b"                       # shoe sizes "uk 9"
    r"|\b\d+\s*(?:gb|tb|mb|ml|ltr|litre|liter|mah|inch|cm|mm|oz|kg|g|w|wh)\b",  # units
    re.IGNORECASE,
)

# ── Language script detectors ─────────────────────────────────────────────────
_LANG_SCRIPT: list[tuple[re.Pattern, str]] = [
    (re.compile(r"[ഀ-ൿ]"), "ml"),   # Malayalam
    (re.compile(r"[஀-௿]"), "ta"),   # Tamil
    (re.compile(r"[ఀ-౿]"), "te"),   # Telugu
    (re.compile(r"[ಀ-೿]"), "kn"),   # Kannada
    (re.compile(r"[ঀ-৿]"), "bn"),   # Bengali
    (re.compile(r"[ऀ-ॿ]"), "hi"),   # Hindi (Devanagari)
    (re.compile(r"[઀-૿]"), "gu"),   # Gujarati
    (re.compile(r"[਀-੿]"), "pa"),   # Punjabi
]

# ── Noise patterns to strip from query ───────────────────────────────────────
_NOISE_RE = re.compile(
    r"\b(?:show\s+me|find\s+me|i\s+want|i\s+need|looking\s+for|"
    r"do\s+you\s+have|can\s+i\s+get|give\s+me|get\s+me|"
    r"search\s+for|please|help\s+me\s+find)\b",
    re.IGNORECASE,
)


@dataclass
class NormalizedQuery:
    """Output of L0 normalizer — consumed by cache and search layers."""
    raw: str                          # original user text
    clean: str                        # normalised search string
    lang: str = "en"                  # detected language code
    min_price: Optional[float] = None # extracted price floor
    max_price: Optional[float] = None # extracted price ceiling
    in_stock_only: bool = False       # user asked for in-stock items
    has_attribute: bool = False       # query pins a colour/size/capacity → skip L2
    tokens: list[str] = field(default_factory=list)  # clean tokenised words
    cache_key: str = ""               # deterministic key for L1 lookup

    def is_empty(self) -> bool:
        return not self.clean.strip()


def normalize(raw_query: str) -> NormalizedQuery:
    """Normalise a raw user query. Pure function, ~0.5ms, no I/O."""
    if not raw_query:
        return NormalizedQuery(raw="", clean="", cache_key="__empty__")

    text = raw_query.strip()

    # 1. Detect language before lowercasing (script detection needs original case)
    lang = _detect_language(text)

    # 2. Unicode normalise (NFC) — handles accented chars consistently
    text = unicodedata.normalize("NFC", text)

    # 3. Lowercase
    text = text.lower()

    # 4. Extract price filters before stripping numbers
    min_price, max_price = _extract_prices(text)

    # 5. Extract stock hint + discriminating-attribute flag (before stripping)
    in_stock_only = bool(_IN_STOCK_RE.search(text))
    has_attribute = bool(_ATTRIBUTE_RE.search(text))

    # 6. Strip noise phrases ("show me", "i want", "looking for", etc.)
    text = _NOISE_RE.sub(" ", text)

    # 7. Strip price/stock phrases now (they've been captured)
    text = _PRICE_UNDER_RE.sub(" ", text)
    text = _PRICE_OVER_RE.sub(" ", text)
    text = _PRICE_RANGE_RE.sub(" ", text)
    text = _IN_STOCK_RE.sub(" ", text)

    # 8. Remove punctuation except hyphens (needed for "t-shirt", "v-neck")
    text = re.sub(r"[^\w\s\-]", " ", text)

    # 9. Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()

    # 10. Expand synonyms
    for informal, canonical in _SYNONYMS.items():
        text = re.sub(rf"\b{re.escape(informal)}\b", canonical, text)

    # 11. Tokenise (for BM25 and embedding)
    tokens = [t for t in text.split() if len(t) > 1]

    # 12. Build deterministic cache key
    cache_key = _make_cache_key(text, min_price, max_price, in_stock_only)

    return NormalizedQuery(
        raw=raw_query,
        clean=text,
        lang=lang,
        min_price=min_price,
        max_price=max_price,
        in_stock_only=in_stock_only,
        has_attribute=has_attribute,
        tokens=tokens,
        cache_key=cache_key,
    )


# ── Private helpers ───────────────────────────────────────────────────────────

def _detect_language(text: str) -> str:
    for pattern, code in _LANG_SCRIPT:
        if pattern.search(text):
            return code
    return "en"


def _extract_prices(text: str) -> tuple[Optional[float], Optional[float]]:
    min_price: Optional[float] = None
    max_price: Optional[float] = None

    # Range check first — "between 200 and 500"
    range_match = _PRICE_RANGE_RE.search(text)
    if range_match:
        a = float(range_match.group(1).replace(",", ""))
        b = float(range_match.group(2).replace(",", ""))
        min_price, max_price = (a, b) if a <= b else (b, a)
        return min_price, max_price

    under_match = _PRICE_UNDER_RE.search(text)
    if under_match:
        max_price = float(under_match.group(1).replace(",", ""))

    over_match = _PRICE_OVER_RE.search(text)
    if over_match:
        min_price = float(over_match.group(1).replace(",", ""))

    return min_price, max_price


def _make_cache_key(clean: str, min_p: Optional[float], max_p: Optional[float], stock: bool) -> str:
    parts = [clean]
    if min_p is not None:
        parts.append(f"min{int(min_p)}")
    if max_p is not None:
        parts.append(f"max{int(max_p)}")
    if stock:
        parts.append("instock")
    return ":".join(parts)
