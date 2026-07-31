"""Canonical attribute parser — the single source of truth for colour/size.

Store-independent: the SAME functions are used on the query side (extract a
user's colour/size) and on the product side (extract a product's colours/sizes
at ingest time). Matching therefore becomes canonical-token equality instead of
free-text regex, so a new store's "Charcoal Grey", "Navy Blue", "Size: 9 UK",
"Medium/Large" or "42 EU" works with no per-store code.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# ── Canonical colour map ───────────────────────────────────────────────────────
# Alias (lowercase, separators normalised to spaces) -> canonical token.
_COLOR_ALIASES: Dict[str, str] = {
    # black
    "black": "black", "jet black": "black", "off black": "black", "blackout": "black",
    # white
    "white": "white", "off white": "white", "offwhite": "white", "ivory": "white",
    "eggshell": "white", "pearl white": "white", "cream white": "white", "milky white": "white",
    # grey
    "grey": "grey", "gray": "grey", "charcoal grey": "charcoal", "charcoal gray": "charcoal",
    "dark grey": "grey", "dark gray": "grey", "light grey": "grey", "light gray": "grey",
    "slate": "grey", "silver grey": "grey", "silver gray": "grey", "ash grey": "grey",
    "ash gray": "grey", "smoke grey": "grey", "smoke gray": "grey",
    "charcoal": "charcoal",
    # brown / tan / beige / cream
    "brown": "brown", "dark brown": "brown", "light brown": "brown", "coffee": "brown",
    "chocolate": "brown", "espresso": "brown", "chestnut": "brown", "cocoa": "brown",
    "cappuccino": "brown", "taupe": "brown", "mocha": "brown",
    "tan": "tan", "camel": "tan", "khaki": "tan", "caramel": "tan", "sand": "beige",
    "beige": "beige", "sand beige": "beige", "off white cream": "cream",
    "cream": "cream",
    # navy / blue
    "navy": "navy", "navy blue": "navy", "midnight blue": "navy", "midnight": "navy",
    "blue": "blue", "sky blue": "blue", "light blue": "blue", "royal blue": "blue",
    "denim blue": "blue", "cobalt": "blue", "baby blue": "blue", "steel blue": "blue",
    "ice blue": "blue", "pastel blue": "blue", "electric blue": "blue", "ocean blue": "blue",
    # red / maroon / burgundy
    "red": "red", "dark red": "red", "bright red": "red", "crimson": "red",
    "scarlet": "red", "cherry": "red", "brick red": "red", "tomato red": "red",
    "maroon": "maroon", "oxblood": "maroon", "wine": "burgundy", "burgundy": "burgundy",
    "berry": "burgundy", "bordeaux": "burgundy",
    # pink / magenta
    "pink": "pink", "rose": "pink", "hot pink": "pink", "baby pink": "pink",
    "blush": "pink", "salmon": "pink", "dusty pink": "pink", "bubblegum pink": "pink",
    "magenta": "magenta", "fuchsia": "magenta",
    # purple / lavender
    "purple": "purple", "violet": "purple", "plum": "purple", "grape": "purple",
    "lavender": "lavender", "lilac": "lavender",
    # green / olive / teal / cyan
    "green": "green", "dark green": "green", "light green": "green", "lime": "green",
    "emerald": "green", "forest green": "green", "olive green": "green", "sage": "green",
    "mint": "green", "army green": "green", "moss green": "green", "pine green": "green",
    "neon green": "green", "pastel green": "green",
    "olive": "olive",
    "teal": "teal", "turquoise": "teal", "aqua": "teal", "teal green": "teal",
    "cyan": "cyan", "aquamarine": "cyan",
    # yellow / gold
    "yellow": "yellow", "mustard": "yellow", "lemon": "yellow", "sun yellow": "yellow",
    "canary yellow": "yellow", "neon yellow": "yellow",
    "gold": "gold", "golden": "gold", "yellow gold": "gold", "rose gold": "gold",
    "antique gold": "gold", "champagne": "gold", "champagne gold": "gold",
    # orange
    "orange": "orange", "amber": "orange", "tangerine": "orange", "peach": "orange",
    "coral": "orange", "burnt orange": "orange", "rust orange": "orange",
    # silver / copper
    "silver": "silver", "platinum": "silver", "pewter": "silver", "gunmetal": "silver",
    "copper": "copper", "bronze": "copper", "rust": "copper", "terracotta": "copper",
    # multi
    "multicolor": "multi", "multicolour": "multi", "rainbow": "multi",
    "mixed colors": "multi", "mixed colours": "multi", "colourful": "multi",
    "colorful": "multi", "multi color": "multi", "multi colour": "multi",
}

# Pre-built regex: longest aliases first so "navy blue" wins over "blue".
_COLOR_ALIAS_WORDS = sorted(
    (a for a in _COLOR_ALIASES if " " not in a and "-" not in a),
    key=len, reverse=True,
)
_COLOR_PHRASE_WORDS = sorted(
    (a for a in _COLOR_ALIASES if " " in a or "-" in a),
    key=len, reverse=True,
)
_COLOR_PHRASE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(a.replace("-", " ")) for a in _COLOR_PHRASE_WORDS) + r")\b"
)
_COLOR_SINGLE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(a) for a in _COLOR_ALIAS_WORDS) + r")\b"
)

# Tokens that qualify as a colour for the L2-bypass detector / text stripping.
COLOR_TOKENS = tuple(sorted({_COLOR_ALIASES[a] for a in _COLOR_ALIASES}))
_COLOR_TOKEN_RE = re.compile(r"\b(?:" + "|".join(COLOR_TOKENS) + r")\b")

# ── Size handling ───────────────────────────────────────────────────────────────
# Clothing-size words -> canonical letter.
_SIZE_LETTER_MAP = {
    "small": "S", "sm": "S", "s": "S", "xsmall": "XS", "xsmall": "XS",
    "xs": "XS", "extra small": "XS", "xxsmall": "XXS", "xxs": "XXS",
    "medium": "M", "med": "M", "m": "M", "one size": "OS", "onesize": "OS",
    "large": "L", "lg": "L", "l": "L", "xlarge": "XL", "xl": "XL", "extra large": "XL",
    "xxlarge": "XXL", "xxl": "XXL", "2xl": "XXL", "xxxl": "XXXL", "3xl": "XXXL",
    "4xl": "XXXXL", "5xl": "XXXXXL",
}
_SIZE_LETTER_WORDS = sorted(
    (k for k in _SIZE_LETTER_MAP if k.isalpha() and len(k) > 1),
    key=len, reverse=True,
)
_SIZE_LETTER_PHRASE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in _SIZE_LETTER_WORDS) + r")\b"
)
# Single letters are extracted separately (X, S, M, L, XL, XXL already matched).
_SIZE_LETTER_RE = re.compile(r"\b(?:x{0,2}[sl]|m)\b", re.IGNORECASE)

# Numeric sizes: "size 9", "uk 9", "eu 42", "9", "9.5", "9 uk".
# The optional (?:is|are|of)? bridges filler between the label and the number
# so "my size is 9" / "size of 9" extract a size instead of being skipped.
_SIZE_KEYWORD_RE = re.compile(
    r"(?:size|sized?|shoe\s+size|uk|us|eu)\s*(?:is|are|of)?\s*(\d{1,2}(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
_SIZE_LABEL_AFTER_RE = re.compile(
    r"(\d{1,2}(?:\.\d{1,2})?)\s*(?:uk|us|eu|size|sizes|shoe\s+size)\b",
    re.IGNORECASE,
)
_SIZE_STANDALONE_RE = re.compile(r"\b(\d{1,2}(?:\.\d{1,2})?)\b")
_MAX_NUMERIC_SIZE = 60.0

# Free-text numbers that must NOT be read as sizes: ratings ("4.0/5", "4/5",
# "4.5 stars", "rating 4.5"), measurements ("3.8 cm", "heel height 9 inch"),
# percentages and prices. Applied to the RAW text BEFORE separator normalisation
# (normalisation turns "4.0/5" into "4.0 5", which would otherwise re-expose the
# rating numbers to the standalone pass) so a "Rating: 4.0/5" never leaks "4"/"5"
# into a product's sizes[] (which would false-positive on "size 4"/"size 5").
_SIZE_NOISE_RE = re.compile(
    r"\b\d{1,2}(?:\.\d{1,2})?\s*/\s*\d{1,2}\b"
    r"|\b\d{1,2}(?:\.\d{1,2})?\s*(?:out\s+of\s+\d{1,2}|stars?)\b"
    r"|\b(?:rating|rated|ratings?)\b\s*\d{1,2}(?:\.\d{1,2})?\b"
    r"|\b\d{1,2}(?:\.\d{1,2})?\s*(?:cm|mm|inch|inches|centimeters?|millimeters?)\b"
    r"|\b\d+(?:\.\d+)?\s*%"
    r"|[₹$€£]\s*\d+(?:\.\d+)?"
    r"|\b\d+(?:\.\d+)?\s*[₹$€£]"
    r"|\b(?:rs\.?|inr|usd|eur|gbp)\b\s*\d+(?:\.\d+)?",
    re.IGNORECASE,
)

_SEP_RE = re.compile(r"[^a-z0-9.]", re.IGNORECASE)


def _normalise_sep(text: str) -> str:
    """Collapse separators so 'off-white' / 'off_white' / 'off white' match.

    Apostrophes are dropped (men's -> mens) so possessives never leak a
    standalone 's'; dots are preserved so '9.5' is not split into '9' and '5'.
    """
    text = text.lower().replace("'", "").replace("`", "")
    return _SEP_RE.sub(" ", text).strip()


# ── Colour extraction ───────────────────────────────────────────────────────────

def extract_colors(text: Optional[str]) -> List[str]:
    """All canonical colours present in a text blob (unique, deterministic)."""
    if not text:
        return []
    norm = _normalise_sep(text)
    found: List[str] = []
    for m in _COLOR_PHRASE_RE.finditer(norm):
        tok = _COLOR_ALIASES.get(m.group(0).replace("-", " "))
        if tok and tok not in found:
            found.append(tok)
    for m in _COLOR_SINGLE_RE.finditer(norm):
        tok = _COLOR_ALIASES.get(m.group(0))
        if tok and tok not in found:
            found.append(tok)
    return found


def canonical_color(value: Optional[str]) -> Optional[str]:
    """Map a user-supplied colour value to its canonical token (or None)."""
    if not value:
        return None
    norm = _normalise_sep(value)
    for alias, canon in sorted(_COLOR_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True):
        if norm == alias or norm.startswith(alias + " "):
            return canon
    # Fall back to any colour word inside the value ("love tan color" -> tan).
    colors = extract_colors(value)
    return colors[0] if colors else None


def color_tokens_in(text: Optional[str]) -> bool:
    """True when any canonical colour token appears in the text (L2-bypass)."""
    if not text:
        return False
    return bool(_COLOR_TOKEN_RE.search(_normalise_sep(text)))


# ── Size extraction ─────────────────────────────────────────────────────────────

def extract_sizes(text: Optional[str]) -> List[str]:
    """All canonical sizes present in a text blob (unique, deterministic).

    Numeric sizes are kept as plain strings ("9", "9.5", "42"). Clothing sizes
    are normalised to canonical letters ("Medium" -> "M", "XL" -> "XL").
    """
    if not text:
        return []
    norm = _normalise_sep(_SIZE_NOISE_RE.sub(" ", text))
    found: List[str] = []

    def add(tok: str) -> None:
        if tok and tok not in found:
            found.append(tok)

    for m in _SIZE_KEYWORD_RE.finditer(norm):
        add(_normalise_number(m.group(1)))

    for m in _SIZE_LABEL_AFTER_RE.finditer(norm):
        add(_normalise_number(m.group(1)))

    for m in _SIZE_LETTER_PHRASE_RE.finditer(norm):
        add(_SIZE_LETTER_MAP[m.group(0)])

    for m in _SIZE_LETTER_RE.finditer(norm):
        add(m.group(0).upper())

    # Standalone numbers that look like sizes (1-60), excluding price-y numbers.
    for m in _SIZE_STANDALONE_RE.finditer(norm):
        try:
            num = float(m.group(1))
        except ValueError:
            continue
        if 0 < num <= _MAX_NUMERIC_SIZE:
            add(_normalise_number(m.group(1)))

    return found


def _normalise_number(raw: str) -> str:
    """Normalise a numeric size ('09' -> '9', keep '9.5')."""
    try:
        f = float(raw)
    except ValueError:
        return raw
    if f == int(f):
        return str(int(f))
    return f"{f:g}"


_SIZE_LETTER_ALL = "|".join(
    re.escape(k)
    for k in sorted(
        (k for k in _SIZE_LETTER_MAP if " " not in k), key=len, reverse=True,
    )
)


def _paren_letters(raw: Optional[str]) -> List[str]:
    """Canonical letters for parenthesised sizes ('(M)', '(XL)', '(2XL)'). Must
    run on RAW text — separator normalisation strips the parentheses."""
    out: List[str] = []
    if not raw:
        return out
    for m in re.finditer(r"\(" + "(" + _SIZE_LETTER_ALL + r")" + r"\)", raw.lower()):
        canon = _SIZE_LETTER_MAP.get(m.group(1))
        if canon and canon not in out:
            out.append(canon)
    return out


def labeled_size(text: Optional[str]) -> Optional[str]:
    """First LABELLED size in free text ('size 9', 'UK 9', '9 UK', 'Medium',
    '(XL)'). Bare numbers/letters do NOT count — 'Delta-20' or 'Turbo Glide M'
    are model numbers, not sizes, so they must never become a size filter."""
    if not text:
        return None
    paren = _paren_letters(text)
    if paren:
        return paren[0]
    norm = _normalise_sep(_SIZE_NOISE_RE.sub(" ", text))
    m = _SIZE_KEYWORD_RE.search(norm)
    if m:
        return _normalise_number(m.group(1))
    m = _SIZE_LABEL_AFTER_RE.search(norm)
    if m:
        return _normalise_number(m.group(1))
    m = _SIZE_LETTER_PHRASE_RE.search(norm)
    if m:
        return _SIZE_LETTER_MAP[m.group(0)]
    return None


def extract_product_sizes(*, text: str = "", structured: str = "") -> List[str]:
    """Canonical sizes for a PRODUCT row.

    `structured` (attribute option values + variant attribute values) is trusted
    store data, so standalone numbers and letters count ('S', '9', '42',
    'UK 10'). `text` (name/description/tags) is free text, so only LABELLED
    sizes are accepted ('size 9', 'UK 9', '9 UK', 'Medium', '(XL)') — a bare
    'M' in 'Turbo Glide M' or '20' in 'Delta-20' is a model number, not a size.
    """
    found: List[str] = []

    def add(tok: str) -> None:
        if tok and tok not in found:
            found.append(tok)

    for tok in extract_sizes(structured):
        add(tok)

    for canon in _paren_letters(text):
        add(canon)

    norm = _normalise_sep(_SIZE_NOISE_RE.sub(" ", text))
    for m in _SIZE_KEYWORD_RE.finditer(norm):
        add(_normalise_number(m.group(1)))
    for m in _SIZE_LABEL_AFTER_RE.finditer(norm):
        add(_normalise_number(m.group(1)))
    for m in _SIZE_LETTER_PHRASE_RE.finditer(norm):
        add(_SIZE_LETTER_MAP[m.group(0)])

    return found


def canonical_size(value: Optional[str]) -> Optional[str]:
    """Map a user-supplied size value to a canonical token (or None)."""
    if not value:
        return None
    norm = _normalise_sep(value)
    m = _SIZE_KEYWORD_RE.search(norm)
    if m:
        return _normalise_number(m.group(1))
    m = _SIZE_LABEL_AFTER_RE.search(norm)
    if m:
        return _normalise_number(m.group(1))
    m = _SIZE_LETTER_PHRASE_RE.search(norm)
    if m:
        return _SIZE_LETTER_MAP[m.group(0)]
    sizes = extract_sizes(value)
    return sizes[0] if sizes else None


# ── Product-side helper (ingest time) ───────────────────────────────────────────

def parse_product_attributes(
    *,
    name: Optional[str] = "",
    description: Optional[str] = "",
    tags: Optional[str] = None,
    attributes: Optional[Dict[str, List[str]]] = None,
    variants: Optional[List[object]] = None,
) -> Tuple[List[str], List[str]]:
    """Extract canonical colours/sizes from a product for product_cache ingest.

    Consumes the platform-neutral CanonicalProduct shape: attributes map
    (option name -> values) plus variant attribute dicts, falling back to the
    name/description/tags text. Any store whose options/variants carry colour
    or size values — under any label — is handled here once.
    """
    text_parts: List[str] = [name or "", description or "", tags or ""]
    structured_parts: List[str] = []
    if attributes:
        for values in attributes.values():
            if isinstance(values, (list, tuple)):
                structured_parts.extend(str(v) for v in values if v is not None)
            else:
                structured_parts.append(str(values))
    if variants:
        for v in variants:
            attrs = getattr(v, "attributes", None)
            if isinstance(attrs, dict):
                structured_parts.extend(str(x) for x in attrs.values() if x is not None)
    text = " ".join(text_parts)
    structured = " ".join(structured_parts)
    colors = extract_colors(" ".join([text, structured]))
    sizes = extract_product_sizes(text=text, structured=structured)
    return colors, sizes
