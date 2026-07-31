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

from .attributes import canonical_color, canonical_size, labeled_size, COLOR_TOKENS


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
    "dress shoes": "formal shoes",
    "office shoes": "formal shoes",
    "party shoes": "formal shoes",
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

# ── Occasion detection ────────────────────────────────────────────────────────
# Soft relevance signal ("for a wedding", "office wear") — extracted so the
# searchbar/search can surface it as a chip, never a hard DB/storefront filter
# (stores rarely tag products by occasion).
_OCCASIONS: list[tuple[str, re.Pattern]] = [
    ("wedding", re.compile(
        r"\b(?:wedding|weddings|bride|groom|bridal|marriage|shaadi)\b", re.IGNORECASE)),
    ("party", re.compile(
        r"\b(?:party|birthday|anniversary|festive|festival|diwali|christmas|"
        r"new\s*year|celebration|sangeet|reception)\b", re.IGNORECASE)),
    ("office", re.compile(
        r"\b(?:office|interview|corporate|business|workwear|work\s+wear|"
        r"office\s+wear)\b", re.IGNORECASE)),
    ("daily", re.compile(
        r"\b(?:daily|casual|everyday|regular|routine)\b", re.IGNORECASE)),
]

# ── Currency and price word patterns to strip from search query ──
_CURRENCY_WORDS_RE = re.compile(
    r"\b(?:rs|inr|usd|\$|₹|€|£|dollars?|rupees?|pounds?|euros?|bucks?|cents?)\b",
    re.IGNORECASE,
)
_STANDALONE_PRICE_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:rs|inr|usd|\$|₹|€|£|dollars?|rupees?|pounds?|euros?|bucks?)\b|\b(?:rs|inr|usd|\$|₹|€|£)\s*\d+(?:\.\d+)?\b",
    re.IGNORECASE,
)

# ── Discriminating-attribute detector ─────────────────────────────────────────
# Queries that pin a specific colour / size / capacity / model must NOT be served
# from the L2 semantic cache: "red shoes" and "blue shoes" embed >0.92 similar, so
# a fuzzy hit would return the wrong variant. When any of these match, callers skip
# L2 and go straight to L3 (exact L1 cache is still safe — it keys on the full text).
# Colour tokens come from attributes.py (single source of truth).
_ATTRIBUTE_RE = re.compile(
    r"\b(?:" + "|".join(COLOR_TOKENS) + r")\b"                     # colours
    r"|\b(?:xs|s|m|l|xl|xxl|xxxl|small|medium|large)\b"            # clothing sizes
    r"|\b(?:uk|us|eu)\s*\d{1,2}\b"                                 # shoe sizes "uk 9"
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
    r"\b(?:show\s+me|find\s+me|i\s+want|i\s+need|i'?m\s+looking\s+for|looking\s+for|"
    r"do\s+you\s+have|do\s+you\s+sell|can\s+i\s+get|give\s+me|get\s+me|"
    r"search\s+for|please|help\s+me\s+find|tell\s+me\s+about|"
    r"what\s+are\s+the|what\s+are|what\s+is|what'?s|are\s+there\s+any|"
    r"i\s+am\s+looking\s+for)\b",
    re.IGNORECASE,
)

# ── Size/attribute patterns to extract as structured filters (not search text) ──
# Covers "size 9", "size uk 9", "uk 9", "us 9", "eu 42" AND "9 uk" so a labeled
# size never pollutes the storefront query text.
_SIZE_RE = re.compile(
    r"\b(?:(?:size|sized?)\s*(?:uk|us|eu)?|(?:uk|us|eu))\s*(\d{1,2}(?:\.\d+)?)\b"
    r"|\b(\d{1,2}(?:\.\d+)?)\s*(?:uk|us|eu)\b",
    re.IGNORECASE,
)
# Clothing sizes
_CLOTHING_SIZE_RE = re.compile(
    r"\b(xxs?|xs|s|m|l|xl|xxl|xxxl|2xl|3xl|small|medium|large|xsmall|xsm|xlarge)\b",
    re.IGNORECASE,
)
# Leading affirmations/negations a user types before the real query
# ("no i need a watch", "yeah show me watches") — stripped only at the START so a
# meaningful "no" elsewhere is untouched.
_LEADING_FILLER_RE = re.compile(r"^\s*(?:no|yes|yeah|yep|nope|nah|ok|okay|sure|hmm|umm|well)\b[\s,]*", re.IGNORECASE)


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
    size: Optional[str] = None        # extracted size (e.g., "9", "M", "UK 9")
    color: Optional[str] = None       # extracted colour (e.g., "tan", "black")
    occasion: Optional[str] = None    # soft intent hint (e.g., "wedding", "office")

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

    # 5. Extract size BEFORE stripping (so we can pass it as structured filter)
    size = _extract_size(text)

    # 5b. Extract colour BEFORE stripping (e.g., "tan", "black dress" → "black")
    color = _extract_color(text)

    # 5c. Extract occasion BEFORE stripping ("for a wedding" → "wedding")
    occasion = _extract_occasion(text)

    # 6. Extract stock hint + discriminating-attribute flag (before stripping)
    in_stock_only = bool(_IN_STOCK_RE.search(text))
    has_attribute = bool(_ATTRIBUTE_RE.search(text))

    # 7. Strip noise phrases ("show me", "i want", "looking for", etc.) and a
    #    leading affirmation/negation ("no i need…", "yeah show…").
    text = _LEADING_FILLER_RE.sub("", text)
    text = _NOISE_RE.sub(" ", text)
    text = re.sub(r"\b(?:i|love|prefer|like|favourite|favorite)\b", " ", text)

    # 8. Strip price/stock phrases now (they've been captured)
    text = _PRICE_UNDER_RE.sub(" ", text)
    text = _PRICE_OVER_RE.sub(" ", text)
    text = _PRICE_RANGE_RE.sub(" ", text)
    text = _STANDALONE_PRICE_RE.sub(" ", text)
    text = _IN_STOCK_RE.sub(" ", text)

    # 9. Strip currency words and symbols (they're captured as price filters)
    text = _CURRENCY_WORDS_RE.sub(" ", text)
    text = re.sub(r"[₹$€£]", " ", text)

    # 10. Strip size patterns from search text (passed as structured filter)
    text = _SIZE_RE.sub(" ", text)
    text = _CLOTHING_SIZE_RE.sub(" ", text)
    text = re.sub(r"\b(?:size|sized?)\b", " ", text)

    # 10b. Strip colour words + colour filler from search text (colour is passed
    # as a structured filter, so it must not pollute the storefront query).
    text = re.sub(
        r"\b(?:" + "|".join(COLOR_TOKENS) + r"|colour|color|love)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )

    # 11. Remove punctuation except hyphens (needed for "t-shirt", "v-neck")
    text = re.sub(r"[^\w\s\-]", " ", text)

    # 12. Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()

    # 13. Expand synonyms
    for informal, canonical in _SYNONYMS.items():
        text = re.sub(rf"\b{re.escape(informal)}\b", canonical, text)

    # 14. Tokenise (for BM25 and embedding)
    tokens = [t for t in text.split() if len(t) > 1]

    # 15. Build deterministic cache key
    cache_key = _make_cache_key(text, min_price, max_price, in_stock_only, size, color)

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
        size=size,
        color=color,
        occasion=occasion,
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


def _make_cache_key(clean: str, min_p: Optional[float], max_p: Optional[float], stock: bool, size: Optional[str] = None, color: Optional[str] = None) -> str:
    parts = [clean]
    if min_p is not None:
        parts.append(f"min{int(min_p)}")
    if max_p is not None:
        parts.append(f"max{int(max_p)}")
    if stock:
        parts.append("instock")
    if size:
        parts.append(f"size{size.replace(' ', '')}")
    if color:
        parts.append(f"color{color}")
    return ":".join(parts)


def _extract_size(text: str) -> Optional[str]:
    """Extract a LABELLED size from the query ('size 9' → '9', 'UK 9' → '9',
    'medium' → 'M'). Bare numbers are ignored here ('Delta-20' is a model, not
    size 20); bare tool-arg values are still canonicalised by canonical_size()."""
    return labeled_size(text)


def _extract_color(text: str) -> Optional[str]:
    """Extract + canonicalise the query's colour ('charcoal grey' → 'grey')."""
    return canonical_color(text)


def _extract_occasion(text: str) -> Optional[str]:
    """Extract the query's occasion hint ('for a wedding' → 'wedding')."""
    for label, pattern in _OCCASIONS:
        if pattern.search(text):
            return label
    return None
