"""Input/output guardrails — content filtering, PII redaction, topic focus.

POST-VALIDATION pipeline (the hallucination killer):
  check_input  → sanitise user text, block off-topic before LLM call
  check_output → verify every product_id / price came from retrieved data,
                 strip PII, enforce language match, retry hook if checks fail
"""
from __future__ import annotations

import difflib
import logging
import re
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# ── Off-topic keyword blocklist ───────────────────────────────────────────────
# If the input is clearly off-topic, we reject before sending to the LLM.
# IMPORTANT: only UNIVERSALLY off-topic patterns belong here — this regex runs for
# EVERY tenant before the LLM intent classifier. Category-specific topics (food,
# medical, books, …) must NOT be listed: a food/cooking pattern blocked a spice or
# kitchen store's core sales questions ("which masala to cook biryani?"), and a
# medical pattern broke pharmacy stores. Those are owned by the intent classifier
# (agent/classifier.py), which sees store context and classifies off_topic per case.
_OFF_TOPIC_PATTERNS: List[re.Pattern] = [re.compile(p, re.IGNORECASE) for p in [
    r"\b(news|politics|election|vote|president|minister|government)\b",
    r"\b(weather|forecast|temperature|humidity)\b",
    r"\b(coding|debug|programming|python|javascript|html|css|sql)\b",
    r"\b(stock market|crypto|bitcoin|ethereum|trading|invest)\b",
    r"\b(movie|film|series|netflix|spotify|youtube)\b",
    r"\b(translate|translation|grammar|essay|poem|story)\b",
    r"\b(gpt|openai|gemini|claude|llm|artificial intelligence|chatgpt)\b",
]]

# ── PII patterns to strip from outputs ───────────────────────────────────────
_PII_PATTERNS: List[tuple[re.Pattern, str]] = [
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Z|a-z]{2,}\b"), "[email]"),
    (re.compile(r"\b(?:\+91[\-\s]?)?[6-9]\d{9}\b"), "[phone]"),           # Indian mobile
    (re.compile(r"\b\d{10,12}\b"), "[phone]"),                             # generic long number
    (re.compile(r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b"), "[card]"),  # card numbers
    (re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b"), "[pan]"),                  # PAN card
    (re.compile(r"\b\d{12}\b"), "[aadhaar]"),                              # Aadhaar
]

# ── Inline-price stripper ─────────────────────────────────────────────────────
# Three sub-patterns. Each is best-effort, not complete:
#   1. Currency-symbol prefix:  ₹14,999 / $9.99 / ₹15k / ₹1.5k
#   2. Context-anchored bare:   costs 14,999 / priced at 9.99 / only 15,000
#      Misses novel phrasings ("at 14999", "around 15000") — symbol (1) and
#      unit (3) patterns are the reliable catches.
#   3. Trailing currency word:  999 rupees / 14999 rs / 15k rupees / 2 lakh
# Bare integers without context (sizes "42", counts "2 items") are excluded.
_INLINE_PRICE_RE = re.compile(
    r"(?:"
    r"[₹$€£¥]\s*[\d,]+(?:\.\d{1,2})?(?:[kK])?"                                                         # 1. symbol prefix (incl. decimals → "$120.0" strips whole)
    r"|(?:costs?\s+|priced?\s+at\s+|for\s+(?:just\s+|only\s+)?|only\s+|just\s+)[₹$€£¥]?\s*[\d,]+(?:\.\d{1,2})?"  # 2. context-anchored (eats the lead-in word + optional symbol)
    r"|[\d,]+(?:\.\d{1,2})?(?:[kK])?\s*(?:rupees?|rs\.?|inr|dollars?|usd|euros?|eur|pounds?|gbp|lakh)" # 3. trailing unit
    r")",
    re.IGNORECASE,
)

# ── Stock-count stripper ──────────────────────────────────────────────────────
# Best-effort: catches "we have 3 left", "only 5 in stock", "3 units available".
# Misses "there are 3", "stock is down to 2" etc. — acceptable given the prompt
# already forbids exact quantities. Monitor via warning log in strip_inline_prices.
_INLINE_STOCK_RE = re.compile(
    r"(?:\s*(?:with|and|,|—|-)\s+)?"  # consume a leading connector so "available with 3 left" → "available"
    r"(?:(?:only\s+|just\s+)?(?:have|has|got)\s+\d+\s+(?:left|remaining|in\s+stock)"
    r"|(?:only\s+|just\s+)?\d+\s+(?:left|remaining|units?\s+(?:left|available|in\s+stock)))",
    re.IGNORECASE,
)

# ── Language code → script/keyword detectors ─────────────────────────────────
_LANG_SCRIPT_RE: Dict[str, re.Pattern] = {
    "hi": re.compile(r"[ऀ-ॿ]"),   # Devanagari
    "ml": re.compile(r"[ഀ-ൿ]"),   # Malayalam
    "ta": re.compile(r"[஀-௿]"),   # Tamil
    "te": re.compile(r"[ఀ-౿]"),   # Telugu
    "bn": re.compile(r"[ঀ-৿]"),   # Bengali
    "kn": re.compile(r"[ಀ-೿]"),   # Kannada
    "gu": re.compile(r"[઀-૿]"),   # Gujarati
    "pa": re.compile(r"[਀-੿]"),   # Punjabi/Gurmukhi
}


# ═══════════════════════════════════════════════════════════════════════════════
# INPUT GUARDRAIL
# ═══════════════════════════════════════════════════════════════════════════════

class InputBlocked(ValueError):
    """Raised when check_input decides the message is off-topic."""
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def check_input(text: str) -> str:
    """Return sanitised text or raise InputBlocked if clearly off-topic.

    Called BEFORE any LLM call. Fast (regex only, ~0ms).
    """
    if not text or not text.strip():
        return text

    stripped = text.strip()

    # 1. Hard-block obvious off-topic patterns
    for pattern in _OFF_TOPIC_PATTERNS:
        if pattern.search(stripped):
            logger.info("Input blocked (off-topic pattern=%s): %.60s", pattern.pattern[:30], stripped)
            raise InputBlocked(f"off_topic:{pattern.pattern[:30]}")

    # 2. Strip PII from user input before logging / storing
    sanitised = _redact_pii(stripped)

    return sanitised


# ── Size / attribute normalisation map ───────────────────────────────────────
# Prevents false-positives when the product stores "Medium" but the LLM writes "M".
_SIZE_ABBREV: Dict[str, str] = {
    "xs": "extra small", "xsmall": "extra small",
    "s": "small",
    "m": "medium",
    "l": "large",
    "xl": "extra large", "xlarge": "extra large",
    "xxl": "extra extra large", "2xl": "extra extra large",
    "xxxl": "3xl",
}


_SIZE_REGION_PREFIX_RE = re.compile(r"^(uk|eu|us)\s+(\d+)$", re.IGNORECASE)


def _normalize_attr(value: str) -> str:
    """Lowercase + expand common size abbreviations for comparison.

    Also strips UK/EU/US region prefixes from numeric sizes so that
    "uk 6" normalizes to "6" and matches a stored attribute of "6".
    """
    v = value.strip().lower()
    m = _SIZE_REGION_PREFIX_RE.match(v)
    if m:
        v = m.group(2)
    return _SIZE_ABBREV.get(v, v)


# ═══════════════════════════════════════════════════════════════════════════════
# OUTPUT GUARDRAIL  (post-validation — the hallucination killer)
# ═══════════════════════════════════════════════════════════════════════════════

class OutputValidationError(ValueError):
    """Raised when check_output finds a hallucination."""
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


# A "specific product mention": a Capitalised word (or a brand-cased word like
# "iPhone"/"G-Shock") followed by 1+ more Capitalised/number words — e.g.
# "Casio G-Shock Diver", "UltraSound X50", "iPhone 13". Used to detect fabricated
# product names in check_output's name-grounding check.
_PRODUCT_MENTION_RE = re.compile(r"\b([A-Z][\w-]*(?:\s+[A-Z0-9][\w-]*)+)\b")

# A product mention preceded by these (or starting with one) is a NEGATION — the
# agent saying it does NOT have something — which is never a hallucination.
_NEGATION_CONTEXT_RE = re.compile(
    r"(?:\bno\b|\bnot\b|n't|\bnever\b|\bwithout\b|\bsorry\b|unavailable|out of stock|"
    r"don'?t have|do not have|couldn'?t find|could not find|no longer|"
    # negation + a stocking verb between it and the product ("we don't carry X",
    # "we don't stock/sell/offer X", "doesn't currently have X").
    r"(?:don'?t|do not|doesn'?t|does not|couldn'?t|could not|can'?t|cannot|won'?t)"
    r"(?:\s+\w+){0,3}\s+(?:carry|carrying|stock|stocking|sell|selling|offer|offering|have|has)|"
    r"we don'?t|isn'?t|aren'?t)"
    # Allow an optional determiner between the negated verb and the product, so
    # "we don't carry the Rolex GMT2" is recognised as negation, not a fabrication.
    r"(?:\s+(?:the|a|an|any|that|those|these|some))?\s*$",
    re.IGNORECASE,
)
_LEADING_NEGATION_RE = re.compile(r"^(?:no|not|never|without|any|these|those|our)\b", re.IGNORECASE)

# A "model-number" token: letter+digit, digit+letter, or 2+ digits (e.g. "X50",
# "S24", "128gb", "13"). Used to split a product mention into the digit-free path
# (P1-8 fuzzy match vs full names) and the model-number path (P1-8b: every model
# token must appear literally in the retrieved name-token set).
_MODEL_TOKEN_RE = re.compile(r"(?:[a-z][0-9]|[0-9][a-z]|[0-9]{2,})")

# Generic words that appear in many product names ("Edition", "Pro", "Max"). They
# must NOT count as a name match — otherwise a fabricated "iPhone 13 Blue Edition"
# matches a real "...Aviation Edition" just on the shared word "Edition". Excluded
# from name tokens on BOTH sides so only DISTINCTIVE tokens (brand/model) match.
_GENERIC_NAME_TOKENS = frozenset({
    # product-name filler
    "edition", "pro", "max", "plus", "series", "new", "set", "pack", "kit",
    "mini", "lite", "ultra", "premium", "the", "and", "for", "with", "size",
    "color", "colour", "version", "model", "type", "style", "classic", "special",
    # colors
    "black", "white", "red", "blue", "green", "grey", "gray", "yellow", "pink", "purple", "orange", "brown", "navy", "beige", "cream", "olive", "tan", "gold", "silver",
    # generic product terms — fashion
    "shoes", "shoe", "sneaker", "sneakers", "clothing", "apparel", "shirt", "shirts", "pants", "jeans", "jacket", "jackets", "coat", "coats", "boots", "sandals", "slippers", "footwear", "item", "items", "product", "products", "selection", "collection", "brand", "brands", "running", "walking", "gym", "sports", "fashion", "men", "mens", "women", "womens", "boy", "boys", "girl", "girls", "kid", "kids", "child", "children", "size", "sizes",
    # generic product terms — furniture / home
    "chair", "chairs", "table", "tables", "sofa", "sofas", "bed", "beds", "desk", "desks", "cabinet", "cabinets", "shelf", "shelves", "lamp", "lamps", "furniture", "home", "decor", "decoration",
    # generic product terms — electronics / appliances
    "appliance", "appliances", "device", "devices", "machine", "machines", "tool", "tools", "electronic", "electronics", "gadget", "gadgets",
    # generic product terms — apparel extended
    "dress", "dresses", "gown", "gowns", "fabric", "fabrics", "material", "materials", "wear", "outfit", "outfits",
    # common commerce / policy / English words that appear Title-Cased but are NOT
    # products (e.g. "Cash On Delivery", "Free Shipping", "Best Seller") — excluded
    # so they never look like a fabricated product name.
    "cash", "delivery", "shipping", "free", "order", "orders", "return",
    "returns", "refund", "payment", "payments", "card", "checkout", "warranty",
    "exchange", "policy", "support", "service", "sale", "offer", "deal",
    "discount", "coupon", "gift", "voucher", "online", "store", "shop", "today",
    "now", "week", "day", "available", "option", "options", "best", "seller",
    "you", "your", "our", "this", "that", "here", "want", "add", "cart",
})

_GREETING_CONTEXT_RE = re.compile(
    r"\b(hey|hi|hello|dear|welcome|good\s+morning|good\s+afternoon|good\s+evening|thanks?|thank\s+you)\b",
    re.IGNORECASE,
)


def check_output(
    text: str,
    *,
    retrieved_product_ids: Optional[Set[Any]] = None,
    retrieved_prices: Optional[Set[str]] = None,
    retrieved_attributes: Optional[Set[str]] = None,
    retrieved_names: Optional[Set[str]] = None,
    retrieved_full_names: Optional[Set[str]] = None,
    retrieved_stock: Optional[Dict[str, bool]] = None,
    detected_language: str = "en",
    allow_retry: bool = True,
    user_query: Optional[str] = None,
) -> str:
    """Validate LLM output against retrieved data. Return cleaned text or raise.

    Six checks in order:
      1. product IDs ∈ retrieved set           (raises → retry)
      2. prices match retrieved data            (raises → retry)
      3. no invented attribute values           (raises → retry)
      4. PII strip                              (mutates cleaned text)
      4b. inline price/stock-count strip        (mutates cleaned text — structural enforcement)
      5. language matches detected language     (raises → retry)
      6. stock-status matches retrieved data    (raises → retry)

    On failure raises OutputValidationError so the caller can retry with a
    stricter prompt or return a safe fallback.
    """
    if not text or not text.strip():
        return text

    cleaned = text.strip()

    # ── Check 1: product IDs mentioned must be in the retrieved set ──────────
    if retrieved_product_ids:
        # LLM sometimes leaks internal product IDs in its text like "ID: 1234"
        mentioned_ids = set(re.findall(r"\bID[:\s]+(\d+)\b", cleaned, re.IGNORECASE))
        unknown = mentioned_ids - {str(pid) for pid in retrieved_product_ids}
        if unknown:
            msg = f"hallucinated product IDs: {unknown}"
            logger.warning("Output validation FAIL — %s", msg)
            if allow_retry:
                raise OutputValidationError(msg)
            # Non-retry mode: strip the ID references rather than raising
            for uid in unknown:
                cleaned = re.sub(rf"\bID[:\s]+{re.escape(uid)}\b", "", cleaned)

    # ── Check 1b: product NAMES must come from retrieved data ─────────────────
    # Catches fabricated product names ("UltraSound X50") that pass the ID/price
    # checks. Runs whether or not a search returned rows — including the empty case,
    # where naming a model-numbered product is itself the hallucination. VERY
    # conservative to avoid false positives: a mention is flagged only when it
    # contains a model-number token AND is NOT in a negation context. Saying you
    # DON'T have something ("No Casio G-Shock watches are available") is never a
    # hallucination, so negated phrases are skipped entirely.
    names_tok: Set[str] = (retrieved_names or set()).copy()
    if user_query:
        user_toks = [
            t for t in re.findall(r"[a-z0-9]+", user_query.lower())
            if len(t) > 2 and t not in _GENERIC_NAME_TOKENS
        ]
        names_tok.update(user_toks)

    full_names: Set[str] = retrieved_full_names or set()
    # Run even when grounding is empty: a zero-result retrieval is NOT a license to
    # name products. With no grounding, a non-negated mention carrying a model-number
    # token (e.g. "Audemars Piguet X1007") cannot be backed by retrieved data — the
    # model-number path below flags it (empty names_tok ⇒ no token matches). The
    # digit-free fuzzy path stays gated on full_names (no anchor to compare against),
    # and negation/greeting text carries no model-number mention, so plain prose is
    # not over-flagged.
    grounding_empty = not names_tok and not full_names
    if names_tok or full_names or grounding_empty:
        for m in _PRODUCT_MENTION_RE.finditer(cleaned):
            phrase = m.group(1)
            # Skip negation or greeting context: "no/not/don't/can't/couldn't/without/hey/hi/hello/thanks …"
            # just before the phrase, or the phrase itself leading with a negation or greeting word.
            prefix = cleaned[max(0, m.start(1) - 28):m.start(1)].lower()
            if (
                _NEGATION_CONTEXT_RE.search(prefix)
                or _LEADING_NEGATION_RE.match(phrase)
                or _GREETING_CONTEXT_RE.search(prefix)
                or _GREETING_CONTEXT_RE.match(phrase)
            ):
                continue
            toks = [
                t for t in re.findall(r"[a-z0-9]+", phrase.lower())
                if len(t) > 2 and t not in _GENERIC_NAME_TOKENS
            ]
            if not toks:
                # All tokens were generic (e.g. "Table Lamp", "Dress Shirt", "Wooden Chair").
                # Can't do token-level matching, but the full phrase may still be a real
                # product name. Fall back to SequenceMatcher against full names.
                if not full_names:
                    continue
                all_phrase_toks = [t for t in re.findall(r"[a-z0-9]+", phrase.lower()) if len(t) > 2]
                if len(all_phrase_toks) < 2:
                    continue
                joined = " ".join(all_phrase_toks)
                hallucinated = True
                for name in full_names:
                    ratio = difflib.SequenceMatcher(None, joined, name).ratio()
                    if ratio >= 0.60:
                        hallucinated = False
                        break
                if hallucinated:
                    msg = f"hallucinated product name: {phrase!r}"
                    logger.warning("Output validation FAIL — %s", msg)
                    if allow_retry:
                        raise OutputValidationError(msg)
                continue

            model_toks = [t for t in toks if _MODEL_TOKEN_RE.search(t)]
            hallucinated = False
            if model_toks:
                # ── Model-number path ──────────────────────────────────────────
                # Keep the original token-match sanity check (some brand/model token
                # must be from retrieved data) AND require EVERY model-number token to
                # appear literally in the retrieved TOKEN set — so "Galaxy S25" is
                # flagged even when "galaxy"/"s24" were retrieved (P1-8b). Runs against
                # retrieved_names (tokens) ONLY — never the full-name set.
                if not any(t in names_tok for t in toks):
                    hallucinated = True
                elif not all(mt in names_tok for mt in model_toks):
                    hallucinated = True
            else:
                # ── Digit-free path (P1-8) ─────────────────────────────────────
                # Fuzzy match against the retrieved FULL names. Two strategies:
                #   a) Token overlap — the LLM mentions a subset of the product's
                #      distinctive tokens (handles abbreviation/reordering).
                #   b) Prefix match — the LLM's phrase is a leading substring of a
                #      full name (natural conversational truncation of long names).
                #   c) SequenceMatcher ratio as fallback.
                # Threshold 0.60 is intentionally lenient — multi-tenant ecommerce
                # has long descriptive names that the LLM naturally abbreviates for
                # speech (e.g. "Robbie jones Casual Sneakers Canvas Outwear…" →
                # "Robbie Jones Casual Sneakers"). False accepts are acceptable:
                # Check 1c (empty-retrieval) and Check 2 (price) catch the rest.
                if full_names and len(toks) >= 2:
                    joined = " ".join(toks)
                    said_set = set(toks)
                    hallucinated = True
                    for name in full_names:
                        all_toks = set(re.findall(r"[a-z0-9]+", name))
                        overlap = len(said_set & all_toks) / len(said_set)
                        if overlap >= 0.60:
                            hallucinated = False
                            break
                        if name.startswith(joined):
                            hallucinated = False
                            break
                        ratio = difflib.SequenceMatcher(None, joined, name).ratio()
                        if ratio >= 0.60:
                            hallucinated = False
                            break

            if hallucinated:
                msg = f"hallucinated product name: {phrase!r}"
                logger.warning("Output validation FAIL — %s", msg)
                if allow_retry:
                    raise OutputValidationError(msg)
                break  # non-retry: stop at first; caller will re-ground or fall back

    # ── Check 1c: empty-retrieval guard ───────────────────────────────────────
    # If no products were retrieved at all, any product-like mention (price,
    # stock claim, availability) is a hallucination — the LLM has nothing
    # grounded to talk about.
    no_retrieval = not retrieved_product_ids and not retrieved_full_names
    if no_retrieval:
        has_product_claim = bool(re.search(r"(?:in stock|available|price|₹|costs?|rupees)", cleaned, re.IGNORECASE))
        if has_product_claim:
            msg = "hallucinated product mention without any retrieved products"
            logger.warning("Output validation FAIL — %s", msg)
            if allow_retry:
                raise OutputValidationError(msg)

    # ── Check 2: prices must come from retrieved data ─────────────────────────
    # Normalize commas and spaces before comparing so "₹12,499" matches "₹12499".
    # Also strip trailing ".0" so "₹484" matches "₹484.0".
    if retrieved_prices:
        mentioned_prices_raw = re.findall(r"[₹$€£¥]\s*[\d,]+(?:\.\d{1,2})?", cleaned)
        mentioned_prices = set()
        for p in mentioned_prices_raw:
            p = re.sub(r"[\s,]", "", p)
            mentioned_prices.add(p)
            mentioned_prices.add(re.sub(r"\.0$", "", p))
        # Symbol-less / currency-WORD prices: "Rs 9999" / "INR 9999" (prefix)
        for _m in re.findall(r"(?:rs\.?|inr)\s*([\d,]+(?:\.\d{1,2})?)", cleaned, re.IGNORECASE):
            p = re.sub(r"[\s,]", "", _m)
            mentioned_prices.add(p)
            mentioned_prices.add(re.sub(r"\.0$", "", p))
        # Suffix: "9999 rupees" / "9999 rs"
        for _m in re.findall(r"([\d,]+(?:\.\d{1,2})?)\s*(?:rupees?|rs\b|inr)", cleaned, re.IGNORECASE):
            p = re.sub(r"[\s,]", "", _m)
            mentioned_prices.add(p)
            mentioned_prices.add(re.sub(r"\.0$", "", p))
        normalized_retrieved = set()
        for p in retrieved_prices:
            p = re.sub(r"[\s,]", "", p)
            normalized_retrieved.add(p)
            normalized_retrieved.add(re.sub(r"\.0$", "", p))
        unknown_prices = mentioned_prices - normalized_retrieved
        if unknown_prices:
            msg = f"hallucinated prices: {unknown_prices}"
            logger.warning("Output validation FAIL — %s", msg)
            if allow_retry:
                raise OutputValidationError(msg)

    # ── Check 3: no invented attribute values ────────────────────────────────
    # Only runs when we have explicit attribute data from retrieved products.
    # Normalizes common size abbreviations (M→medium, L→large, XL→extra large)
    # to prevent false positives when the store stores full names but LLM uses
    # abbreviations. Single invented values are flagged — a fake colour/size is
    # as harmful as a fake price.
    if retrieved_attributes:
        attr_mentions = set(re.findall(
            r"\b(XS|S|M|L|XL|XXL|XXXL"
            r"|\d+(?:\s*(?:cm|mm|inch|inches|kg|g|ml))"
            r"|(?:red|blue|green|black|white|grey|gray|yellow|pink|purple|orange|brown|navy|beige|cream)"
            r"|(?:uk|eu|us)\s*\d+\b)",
            cleaned,
            re.IGNORECASE,
        ))
        normalized_retrieved = {_normalize_attr(a) for a in retrieved_attributes}
        # Ground attributes against ALL words in retrieved product names to prevent
        # false-positives for attributes that exist in product titles but aren't
        # formally stored in structured taxonomies. Includes generic tokens so
        # color words appearing in product names (e.g. "White" in "...Shoes - White")
        # are never flagged as invented.
        name_words = set()
        for fn in (retrieved_full_names or set()):
            name_words.update(re.findall(r"[a-z0-9]+", fn.lower()))
        # Also include user query tokens so the customer's own words ("brown shoes")
        # are not flagged when the LLM repeats them.
        if user_query:
            name_words.update(re.findall(r"[a-z0-9]+", user_query.lower()))

        # Lowercase and deduplicate mentions to prevent capitalization differences
        # (e.g. {'black', 'Black'}) from being counted as multiple invented attributes.
        invented = {
            _normalize_attr(v) for v in attr_mentions
            if len(str(v).strip()) >= 2
        }
        invented = {
            inv for inv in invented
            if inv not in normalized_retrieved and inv not in name_words
        }
        if len(invented) >= 1:
            msg = f"potentially invented attribute values: {invented}"
            logger.warning("Output validation FAIL — %s", msg)
            if allow_retry:
                raise OutputValidationError(msg)

    # ── Check 4: strip leaked PII ────────────────────────────────────────────
    cleaned = _redact_pii(cleaned)

    # ── Check 4b: strip inline prices/stock counts (structural enforcement) ───
    cleaned = strip_inline_prices(cleaned)

    # ── Check 5: language matches detected language ───────────────────────────
    # Only enforce for non-English where we can detect script.
    if detected_language in _LANG_SCRIPT_RE and detected_language != "en":
        script_re = _LANG_SCRIPT_RE[detected_language]
        # If the expected script is completely absent and the response is long,
        # the LLM replied in the wrong language.
        if not script_re.search(cleaned) and len(cleaned) > 80:
            msg = f"language mismatch: expected={detected_language}, response appears to be in a different script"
            logger.warning("Output validation FAIL — %s", msg)
            if allow_retry:
                raise OutputValidationError(msg)

    # ── Check 6: stock-status matches retrieved data ──────────────────────────
    # Detects "in stock" / "out of stock" / "available" / "sold out" / "only X left"
    # declarations and verifies against stock_map. The LLM must call check_inventory
    # before declaring stock status; this check flags unbacked claims.
    if retrieved_stock and retrieved_product_ids:
        _STOCK_PATTERNS = re.compile(
            r"(in stock|out of stock|available|sold out|back in stock|"
            r"only\s+\d+\s+left|limited stock|low stock|unavailable|"
            r"currently\s+(not\s+)?available|restocked)",
            re.IGNORECASE,
        )
        if _STOCK_PATTERNS.search(cleaned):
            # Find all mentioned product IDs in the same sentence as stock language
            sentences = re.split(r'[.!?\n]+', cleaned)
            for sent in sentences:
                if not _STOCK_PATTERNS.search(sent):
                    continue
                sid_mentions = set(re.findall(r"\b(\d+)\b", sent))
                known_ids = {str(pid) for pid in retrieved_product_ids}
                for sid in sid_mentions:
                    if sid in known_ids and sid in retrieved_stock:
                        expected = retrieved_stock[sid]
                        if expected:
                            # Declaring "out of stock" for something that IS in stock
                            if re.search(r"(out of stock|sold out|unavailable|not available)", sent, re.IGNORECASE):
                                msg = f"stock mismatch: product {sid} is in stock but LLM declared out of stock"
                                logger.warning("Output validation FAIL — %s", msg)
                                if allow_retry:
                                    raise OutputValidationError(msg)
                        else:
                            # Declaring "in stock" for something that IS out of stock
                            if re.search(r"(in stock|available|back in stock|restocked)", sent, re.IGNORECASE):
                                msg = f"stock mismatch: product {sid} is out of stock but LLM declared in stock"
                                logger.warning("Output validation FAIL — %s", msg)
                                if allow_retry:
                                    raise OutputValidationError(msg)

    return cleaned


# ═══════════════════════════════════════════════════════════════════════════════
# VOICE MONITOR  (P1-11 — re-validate Gemini's SPOKEN transcript)
# ═══════════════════════════════════════════════════════════════════════════════

def validate_spoken_text(
    text: str,
    *,
    retrieved_full_names: Optional[Set[str]] = None,
    retrieved_names: Optional[Set[str]] = None,
    retrieved_prices: Optional[Set[str]] = None,
    retrieved_stock: Optional[Dict[str, bool]] = None,
    detected_language: str = "en",
) -> tuple[bool, str]:
    """Monitor the voice channel's SPOKEN transcript against the brain's grounding.

    Pipeline A speaks the brain's answer in Gemini's own words; the relay-verbatim
    system prompt is the PRIMARY guard. This is the secondary MONITOR — it reuses the
    same name/price checks as check_output but NEVER raises into the audio path.

    Returns (is_grounded, cleaned). is_grounded=False means a fabricated product
    name/price was detected in the transcript — the caller substitutes the brain's
    verified text on the displayed bubble and logs the divergence.
    """
    if not text or not text.strip():
        return True, text
    try:
        cleaned = check_output(
            text,
            retrieved_names=retrieved_names,
            retrieved_full_names=retrieved_full_names,
            retrieved_prices=retrieved_prices,
            retrieved_stock=retrieved_stock,
            detected_language=detected_language,
            allow_retry=True,
        )
        return True, cleaned
    except OutputValidationError:
        return False, text


# ═══════════════════════════════════════════════════════════════════════════════
# SAFE FALLBACK  (used when retry also fails)
# ═══════════════════════════════════════════════════════════════════════════════

_SAFE_FALLBACK: Dict[str, str] = {
    "en": "I'm having trouble retrieving that information right now. What product are you looking for?",
    "hi": "Mujhe abhi yeh jaankari laane mein takleef ho rahi hai. Kya dhundhna hai?",
    "ml": "Ith information ippol kittaanബudhimuttundaarunu. Enthu venam?",
    "ta": "Ippo antha thakaval edukkuvathil siramapadugiren. Enna thedugirirkal?",
    "te": "Ee samayamlo aa samacharam teesukuvadam kashtamga undi. Emi kavali?",
    "bn": "Ekhon oi tathya anthe amaar samsya hochhe. Ki lagbe?",
    "kn": "Ippa aa mahiti tegedukoḷḷuvudu kaṣṭavaagide. Yenu beku?",
}


def safe_fallback(language: str = "en") -> str:
    return _SAFE_FALLBACK.get(language, _SAFE_FALLBACK["en"])


# ═══════════════════════════════════════════════════════════════════════════════
# HELPER
# ═══════════════════════════════════════════════════════════════════════════════

def _redact_pii(text: str) -> str:
    for pattern, replacement in _PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def strip_inline_prices(text: str) -> str:
    """Remove inline price and stock-count numbers from LLM output.

    Structural enforcement of the strict price/stock model. The interface renders
    prices from FactBundle/ui_actions; spoken text must contain no numbers. Runs
    after generation regardless of whether the price matches retrieved data —
    complement to (not replacement of) Check 2 which raises for retry.

    Known limitations: mid-sentence stripping leaves grammatical fragments
    ("It costs and ships free"); pattern 2 misses novel phrasings; stock stripper
    misses "there are 3", "down to 2". All acceptable for a last-resort safety net.
    """
    price_hit = _INLINE_PRICE_RE.search(text)
    stock_hit = _INLINE_STOCK_RE.search(text)
    if not price_hit and not stock_hit:
        return text

    logger.warning(
        "Output contained inline price/stock number (stripped — strict model): %.120s", text
    )
    cleaned = _INLINE_PRICE_RE.sub("", text)
    cleaned = _INLINE_STOCK_RE.sub("", cleaned)

    # Post-strip cleanup so removal never leaves grammatical garbage:
    cleaned = re.sub(r"(?<=\s)\.\d{1,2}\b", "", cleaned)          # orphan decimal tail ".0" left by a stripped "$120" or "₹484.0"
    cleaned = re.sub(r"\b(?:with|and)\s+(?:left|remaining|in\s+stock)\b", "", cleaned, flags=re.IGNORECASE)  # "with left"
    cleaned = re.sub(r"(?<![\w])(?:left|remaining)\b(?!\s*\w)", "", cleaned, flags=re.IGNORECASE)  # dangling "left"
    cleaned = re.sub(r"\b(?:available\s+|is\s+)(?:with|and)\s*$", "", cleaned, flags=re.IGNORECASE)  # trailing "available with" / "is with"
    cleaned = re.sub(r"\b(?:only|just)\s*$", "", cleaned, flags=re.IGNORECASE)  # trailing "only" / "just"
    cleaned = re.sub(r"\b(\w+)(\s+\1\b)+", r"\1", cleaned, flags=re.IGNORECASE)  # doubled word "is is" → "is"
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)            # space before punctuation
    cleaned = re.sub(r"([,;:])\s*([.])", r"\2", cleaned)         # orphaned ", ." → "."
    cleaned = re.sub(r"\(\s*\)", "", cleaned)                    # empty parens left by a stripped value
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([.!?])", r"\1", cleaned)
    return cleaned.strip(" ,;:-—")


def build_retrieved_context(
    tool_results: List[Dict[str, Any]],
) -> tuple[Set[str], Set[str], Set[str], Set[str], Set[str], Dict[str, bool]]:
    """Extract product IDs, prices, attribute values, NAME TOKENS, FULL NAMES, and stock map.

    Pass the return value into check_output() so it can validate the LLM
    response against only what was actually retrieved.

    Returns (product_id_set, price_set, attribute_value_set, name_token_set,
    full_name_set, stock_map). name_token_set holds the significant lowercased tokens of every
    retrieved name (used for the model-number literal check); full_name_set holds the
    whole lowercased product names (used for the digit-free fuzzy match). The two are
    kept separate on purpose — check_output never crosses them.
    stock_map maps product_id → True (in stock) / False (out of stock).
    """
    product_ids: Set[str] = set()
    prices: Set[str] = set()
    attributes: Set[str] = set()
    name_tokens: Set[str] = set()
    full_names: Set[str] = set()
    stock_map: Dict[str, bool] = {}

    def _extract_product(p: dict) -> None:
        pid = p.get("id") or p.get("product_id")
        if pid:
            pid_str = str(pid)
            product_ids.add(pid_str)
            # Collect stock status if available
            raw_stock = p.get("in_stock")
            if isinstance(raw_stock, bool):
                stock_map[pid_str] = raw_stock
            elif isinstance(raw_stock, (int, float)):
                stock_map[pid_str] = raw_stock > 0
            stock_qty = p.get("stock_quantity")
            if stock_qty is not None and isinstance(stock_qty, (int, float)):
                if pid_str not in stock_map:
                    stock_map[pid_str] = stock_qty > 0
        # Collect significant name tokens (len>2) AND the whole name for grounding.
        name = str(p.get("name") or "").strip().lower()
        if name:
            full_names.add(name)
            for tok in re.findall(r"[a-z0-9]+", name):
                if len(tok) > 2 and tok not in _GENERIC_NAME_TOKENS:
                    name_tokens.add(tok)
        for price_key in ("price", "regular_price", "sale_price"):
            raw = str(p.get(price_key) or "").strip()
            if raw and raw != "0":
                norm = re.sub(r"[\s,]", "", raw)   # "1,299" → "1299"
                sans_dot0 = re.sub(r"\.0$", "", norm)  # "484.0" → "484"
                prices.add(raw)
                prices.add(norm)
                prices.add(sans_dot0)
                prices.add(f"₹{raw}")
                prices.add(f"₹{norm}")
                prices.add(f"₹{sans_dot0}")
                prices.add(f"₹ {raw}")
        # Flatten attributes dict  {name: [val, val]} or list [{name, options}]
        attrs = p.get("attributes") or {}
        if isinstance(attrs, dict):
            for vals in attrs.values():
                if isinstance(vals, list):
                    for v in vals:
                        if v:
                            raw = str(v).lower()
                            attributes.add(raw)
                            attributes.add(_normalize_attr(raw))
        elif isinstance(attrs, list):
            for attr in attrs:
                if not isinstance(attr, dict):
                    continue
                for vals in (attr.get("options") or []):
                    raw = str(vals).lower()
                    attributes.add(raw)
                    attributes.add(_normalize_attr(raw))
        # Variant-level attributes
        for variant in p.get("variants", []):
            if not isinstance(variant, dict):
                continue
            v_attrs = variant.get("attributes") or {}
            if isinstance(v_attrs, dict):
                for vals in v_attrs.values():
                    if isinstance(vals, list):
                        for v in vals:
                            if v:
                                raw = str(v).lower()
                                attributes.add(raw)
                                attributes.add(_normalize_attr(raw))

    for result in tool_results:
        payload = result if isinstance(result, dict) else {}
        for product in payload.get("products", []):
            if isinstance(product, dict):
                _extract_product(product)
        if "product" in payload and isinstance(payload["product"], dict):
            _extract_product(payload["product"])

    return product_ids, prices, attributes, name_tokens, full_names, stock_map
