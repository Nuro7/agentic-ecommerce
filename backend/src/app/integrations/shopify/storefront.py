"""Shopify Storefront GraphQL service for the Fullscreen Overlay.

Lives *beside* the ~1200-line ShopifyClient (client.py) without touching it: the
overlay browser SPA never sees a Storefront token — the browser talks to
:mod:`app.api.v1.overlay`, which proxies these calls server-side where the token
already lives (tenant DB or env).  Only Storefront 2026-07 operations the
overlay needs are implemented here: product search/detail, cart lifecycle,
discount codes, buyer identity + delivery address, and the checkout URL.

Security invariant: ``storefront_token`` MUST stay server-side.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ...core.http_retry import request_with_retries

logger = logging.getLogger(__name__)

_DEFAULT_API_VERSION = "2026-07"

# ── Natural-language price parsing ─────────────────────────────────────────────
# Shoppers type queries like "formal shoes under 5000" or "watches between 1000
# and 3000". Shopify's Storefront full-text search treats the WHOLE string as
# title/keyword tokens, so the price words ("under", "5000") never match a
# product title and the search returns zero hits — a blank overlay. We lift the
# price intent out into structured ``variants.price`` filters and keep only the
# real keywords for the text match. Plain queries (no price words) pass through
# untouched.

_CURRENCY = r"(?:rs\.?|inr|usd|₹|\$|€|£)?\s*"
_NUM = r"([0-9][0-9,]*(?:\.[0-9]+)?)"

# Order matters: try an explicit range first, then max-only, then min-only.
_RANGE_RE = re.compile(
    r"\bbetween\s+" + _CURRENCY + _NUM + r"\s+(?:and|to|-|–|—)\s+" + _CURRENCY + _NUM,
    re.IGNORECASE,
)
_RANGE_DASH_RE = re.compile(
    r"\b" + _NUM + r"\s*(?:to|-|–|—)\s*" + _CURRENCY + _NUM + r"\b", re.IGNORECASE
)
_MAX_RE = re.compile(
    r"(?:under|below|less than|lesser than|cheaper than|up ?to|at most|no more than|"
    r"within|max(?:imum)?|budget(?: of)?|<=?)\s*" + _CURRENCY + _NUM,
    re.IGNORECASE,
)
_MIN_RE = re.compile(
    r"(?:over|above|more than|greater than|at least|starting (?:at|from)|from|"
    r"min(?:imum)?|>=?)\s*" + _CURRENCY + _NUM,
    re.IGNORECASE,
)
_CURRENCY_WORDS = {"rs", "rs.", "inr", "usd", "$", "₹", "€", "£"}

# ── Variant-option tokens (size / fit) ─────────────────────────────────────────
# "size 9", "uk 9", "size XL" are selectors the shopper picks on the product page
# — NOT descriptors that appear in a product's title/type/tags. Left in the
# keyword string they poison the relevance guard ("size" is in no product's text,
# so every hit gets rejected → blank overlay) and skew Shopify's text ranking. We
# lift them out here, mirroring the voice normalizer (agent/retrieval/normalizer),
# so the overlay grid and the assistant surface the same products.
_OPTION_SIZE_RE = re.compile(
    r"\b(?:(?:sized?)\s*(?:uk|us|eu)?\s*(?:is|are|of)?|(?:uk|us|eu))\s*\d{1,2}(?:\.\d+)?\b"
    r"|\b\d{1,2}(?:\.\d+)?\s*(?:uk|us|eu)\b",
    re.IGNORECASE,
)
_OPTION_WORD_RE = re.compile(
    r"\b(?:sizes?|xxs|xs|s|m|l|xl|xxl|xxxl|2xl|3xl|small|medium|large|xsmall|xlarge)\b",
    re.IGNORECASE,
)


def strip_option_terms(terms: str) -> str:
    """Remove size / variant-selector tokens from a keyword string.

    ``"formal shoes size 9"`` → ``"formal shoes"``; ``"dress size xl"`` → ``"dress"``.
    These are chosen on the product page, never matched against product text, so
    they must never become mandatory qualifiers in the relevance guard.
    """
    if not terms:
        return ""
    cleaned = _OPTION_SIZE_RE.sub(" ", terms)
    cleaned = _OPTION_WORD_RE.sub(" ", cleaned)
    return " ".join(cleaned.split())


def _to_float(s: Optional[str]) -> Optional[float]:
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _fmt_price(n: float) -> str:
    n = float(n)
    return str(int(n)) if n.is_integer() else ("%g" % n)


def parse_price_query(raw: str) -> Dict[str, Any]:
    """Split a shopper query into keyword ``terms`` + optional price bounds.

    Returns ``{"terms": str, "min": float|None, "max": float|None}``. A query
    with no recognizable price language comes back with ``terms == raw.strip()``
    and both bounds ``None`` — so ordinary searches are never altered.
    """
    text = (raw or "").strip()
    if not text:
        return {"terms": "", "min": None, "max": None}

    price_min: Optional[float] = None
    price_max: Optional[float] = None
    spans: List[Tuple[int, int]] = []

    m = _RANGE_RE.search(text) or _RANGE_DASH_RE.search(text)
    if m:
        a, b = _to_float(m.group(1)), _to_float(m.group(2))
        if a is not None and b is not None:
            price_min, price_max = min(a, b), max(a, b)
            spans.append(m.span())
    else:
        mx = _MAX_RE.search(text)
        if mx:
            price_max = _to_float(mx.group(1))
            spans.append(mx.span())
        mn = _MIN_RE.search(text)
        if mn:
            price_min = _to_float(mn.group(1))
            spans.append(mn.span())

    # Remove the matched price phrase(s) from the keyword string.
    if spans:
        kept, last = [], 0
        for s, e in sorted(spans):
            if s < last:  # overlapping match — skip
                continue
            kept.append(text[last:s])
            last = e
        kept.append(text[last:])
        text = "".join(kept)

    text = " ".join(text.split())

    # Strip leftover currency tokens and any stray number that just duplicates a
    # detected bound (e.g. "formal shoes 5000 under 5000" → "formal shoes").
    if price_min is not None or price_max is not None:
        bounds = {
            int(b) for b in (price_min, price_max)
            if b is not None and float(b).is_integer()
        }
        tokens = []
        for tok in text.split():
            # Strip surrounding punctuation ("5000?", "5000." → "5000") so a stray
            # number that duplicates a detected bound is recognised and removed.
            bare = tok.strip(",.?!;:'\"()").lower()
            if bare in _CURRENCY_WORDS:
                continue
            digits = bare.replace(",", "")
            if digits.isdigit() and int(digits) in bounds:
                continue
            tokens.append(tok.strip("?!.,;:"))
        text = " ".join(t for t in tokens if t)

    return {"terms": text.strip(), "min": price_min, "max": price_max}


def build_storefront_query(raw: str) -> Tuple[str, bool]:
    """Build a Shopify Storefront query string from a shopper query.

    Returns ``(query, has_price_filter)``. Keywords stay as free-text tokens;
    price bounds become ``variants.price`` range filters that Shopify's search
    understands.
    """
    parsed = parse_price_query(raw)
    parts: List[str] = []
    terms = strip_option_terms(parsed["terms"])
    if terms:
        parts.append(terms)
    has_filter = False
    if parsed["min"] is not None:
        parts.append("variants.price:>=%s" % _fmt_price(parsed["min"]))
        has_filter = True
    if parsed["max"] is not None:
        parts.append("variants.price:<=%s" % _fmt_price(parsed["max"]))
        has_filter = True
    return " ".join(parts).strip(), has_filter


# ── Result relevance guard ─────────────────────────────────────────────────────
# Shopify's Storefront full-text search is fuzzy / OR-ranked: a query for
# "formal shoes" also returns anything merely containing "shoes" (running,
# casual, sports). For a MULTI-WORD query the extra words are qualifiers the
# shopper actually means ("formal", "leather", "wireless") — silently dropping
# them is exactly the mismatch bug. So we keep only products whose searchable
# text contains EVERY qualifier. A single bare category word ("sneakers") has no
# qualifier to enforce, so we trust Shopify's relevance ranking and don't filter.

_SEARCH_STOPWORDS = {
    "a", "an", "the", "for", "me", "my", "some", "any", "show", "find",
    "want", "need", "please", "with", "in", "of", "and", "or", "to", "get",
    "looking", "look", "search", "buy", "under", "over", "about", "that",
    "this", "these", "those", "is", "are", "you", "have", "do", "can", "at",
    "priced", "price", "cost", "around", "between", "below", "above", "than",
    "less", "more", "rs", "inr",
}
# Generic browse words that don't describe a specific product line. When the
# whole query is generic ("show me bestsellers", "new arrivals") we return the
# store's relevance/best-selling results as-is instead of enforcing qualifiers.
_GENERIC_TOKENS = {
    "best", "seller", "sellers", "bestseller", "bestsellers", "selling",
    "popular", "trending", "top", "new", "arrival", "arrivals", "latest",
    "deal", "deals", "sale", "offer", "offers", "discount", "discounts",
    "product", "products", "item", "items", "everything", "all", "browse",
    "catalog", "catalogue", "store", "shop", "stuff", "things", "something",
    "anything", "cheap", "cheapest", "affordable", "budget",
}


def _significant_terms(terms: str) -> List[str]:
    """Meaningful keyword tokens: lowercased, ≥2 chars, minus stop/generic words."""
    out: List[str] = []
    for tok in (terms or "").lower().split():
        w = tok.strip(",.?!;:'\"()").strip()
        if len(w) < 2 or w in _SEARCH_STOPWORDS or w in _GENERIC_TOKENS:
            continue
        out.append(w)
    return out


def _term_matches(haystack: str, term: str) -> bool:
    """Substring match with light singular/plural tolerance."""
    if term in haystack:
        return True
    if term.endswith("s") and len(term) > 3 and term[:-1] in haystack:
        return True
    if not term.endswith("s") and (term + "s") in haystack:
        return True
    return False


def _product_matches_terms(product: Dict[str, Any], terms: List[str]) -> bool:
    """True when every qualifier term appears in the product's searchable text."""
    if not terms:
        return True
    haystack = " ".join([
        str(product.get("title") or ""),
        str(product.get("product_type") or ""),
        str(product.get("vendor") or ""),
        " ".join(str(t) for t in (product.get("tags") or [])),
    ]).lower()
    return all(_term_matches(haystack, t) for t in terms)


def _within_price(
    product: Dict[str, Any], lo: Optional[float], hi: Optional[float]
) -> bool:
    """Price-bound check. Unknown/zero price is not excluded."""
    try:
        amt = float((product.get("price") or {}).get("amount") or 0)
    except (TypeError, ValueError):
        return True
    if amt <= 0:
        return True
    if lo is not None and amt < lo:
        return False
    if hi is not None and amt > hi:
        return False
    return True

# ── GraphQL constants ─────────────────────────────────────────────────────────

_CART_LINES = """
    lines(first: 50) {
      edges { node {
        id
        quantity
        merchandise {
          ... on ProductVariant {
            id
            title
            price { amount currencyCode }
            product { handle title featuredImage { url altText } }
          }
        }
        cost { totalAmount { amount currencyCode } }
      } }
    }
"""

_CART_FIELDS = f"""
    id
    checkoutUrl
    totalQuantity
    buyerIdentity {{ email countryCode }}
    discountCodes {{ code applicable }}
    cost {{
      subtotalAmount {{ amount currencyCode }}
      totalAmount {{ amount currencyCode }}
    }}
    {_CART_LINES}
"""

_SEARCH_QUERY = """
query StorefrontSearchProducts($query: String!, $first: Int!, $sortKey: ProductSortKeys, $reverse: Boolean) {
  products(query: $query, first: $first, sortKey: $sortKey, reverse: $reverse) {
    edges {
      node {
        id
        handle
        title
        vendor
        productType
        tags
        availableForSale
        featuredImage { url altText }
        priceRange {
          minVariantPrice { amount currencyCode }
          maxVariantPrice { amount currencyCode }
        }
        compareAtPriceRange {
          minVariantPrice { amount currencyCode }
          maxVariantPrice { amount currencyCode }
        }
      }
    }
  }
}
"""

# Review metafield identifiers — probes the common review apps (Shopify native
# rating, Judge.me, Loox, Okendo, Yotpo). Missing ones come back as null and are
# skipped by :func:`_extract_reviews`.
_REVIEW_METAFIELDS = """
    metafields(identifiers: [
      {namespace: "reviews", key: "rating"},
      {namespace: "reviews", key: "rating_count"},
      {namespace: "loox", key: "avg_rating"},
      {namespace: "loox", key: "num_reviews"},
      {namespace: "okendo", key: "summaryData"},
      {namespace: "judgeme", key: "badge"},
      {namespace: "yotpo", key: "reviews_average"},
      {namespace: "yotpo", key: "reviews_count"}
    ]) { namespace key value type }
"""

_PRODUCT_QUERY = f"""
query StorefrontGetProduct($handle: String!) {{
  product(handle: $handle) {{
    id
    handle
    title
    descriptionHtml
    description
    vendor
    productType
    tags
    availableForSale
    featuredImage {{ url altText }}
    images(first: 12) {{ edges {{ node {{ url altText }} }} }}
    priceRange {{
      minVariantPrice {{ amount currencyCode }}
      maxVariantPrice {{ amount currencyCode }}
    }}
    compareAtPriceRange {{
      minVariantPrice {{ amount currencyCode }}
      maxVariantPrice {{ amount currencyCode }}
    }}
    options {{ name values }}
{_REVIEW_METAFIELDS}
    variants(first: 250) {{
      edges {{
        node {{
          id
          title
          sku
          availableForSale
          quantityAvailable
          currentlyNotInStock
          image {{ url altText }}
          price {{ amount currencyCode }}
          compareAtPrice {{ amount currencyCode }}
          selectedOptions {{ name value }}
        }}
      }}
    }}
  }}
}}
"""

_CART_QUERY = (
    "query StorefrontCartGet($cartId: ID!) {\n"
    f"  cart(id: $cartId) {{\n{_CART_FIELDS}  }}\n"
    "}\n"
)

_CART_CREATE_MUTATION = (
    "mutation StorefrontCartCreate($input: CartInput!) {\n"
    "  cartCreate(input: $input) {\n"
    "    cart {\n"
    + _CART_FIELDS
    + "    }\n"
    "    userErrors { field message code }\n"
    "  }\n"
    "}\n"
)

_CART_LINES_ADD_MUTATION = (
    "mutation StorefrontCartLinesAdd($cartId: ID!, $lines: [CartLineInput!]!) {\n"
    "  cartLinesAdd(cartId: $cartId, lines: $lines) {\n"
    "    cart {\n"
    + _CART_FIELDS
    + "    }\n"
    "    userErrors { field message code }\n"
    "  }\n"
    "}\n"
)

_CART_DISCOUNT_UPDATE_MUTATION = (
    "mutation StorefrontCartDiscountCodesUpdate($cartId: ID!, $discountCodes: [String!]) {\n"
    "  cartDiscountCodesUpdate(cartId: $cartId, discountCodes: $discountCodes) {\n"
    "    cart {\n"
    + _CART_FIELDS
    + "    }\n"
    "    userErrors { field message code }\n"
    "  }\n"
    "}\n"
)

_CART_BUYER_UPDATE_MUTATION = (
    "mutation StorefrontCartBuyerIdentityUpdate($cartId: ID!, $buyerIdentity: CartBuyerIdentityInput!) {\n"
    "  cartBuyerIdentityUpdate(cartId: $cartId, buyerIdentity: $buyerIdentity) {\n"
    "    cart {\n"
    + _CART_FIELDS
    + "    }\n"
    "    userErrors { field message code }\n"
    "  }\n"
    "}\n"
)

_CART_ADDRESS_REPLACE_MUTATION = (
    "mutation StorefrontCartDeliveryAddressesReplace($cartId: ID!, $deliveryAddresses: [CartSelectableAddressInput!]!) {\n"
    "  cartDeliveryAddressesReplace(cartId: $cartId, deliveryAddresses: $deliveryAddresses) {\n"
    "    cart {\n"
    + _CART_FIELDS
    + "    }\n"
    "    userErrors { field message code }\n"
    "  }\n"
    "}\n"
)


def _money(amount: Any, currency: Any) -> Dict[str, Any]:
    """Normalize a MoneyV2 into ``{amount: float, currencyCode: str}``."""
    try:
        return {"amount": float(amount or 0), "currencyCode": currency or ""}
    except (TypeError, ValueError):
        return {"amount": 0.0, "currencyCode": currency or ""}


def _node(edge: Dict[str, Any]) -> Dict[str, Any]:
    return edge.get("node") or {}


def _normalize_product(node: Dict[str, Any]) -> Dict[str, Any]:
    price_range = node.get("priceRange") or {}
    min_v = price_range.get("minVariantPrice") or {}
    image = node.get("featuredImage") or (node.get("images") or {}).get("edges") or []
    if image and isinstance(image, list):
        image = image[0].get("node") or {}

    # Real compare-at = the compare-at price of the cheapest variant (falls back to
    # the top of the compare-at range). Only a *higher* compare-at means "on sale".
    compare_range = node.get("compareAtPriceRange") or {}
    cmp_min = compare_range.get("minVariantPrice") or {}
    cmp_max = compare_range.get("maxVariantPrice") or {}
    price_amt = float(min_v.get("amount") or 0)
    compare_amt = float(cmp_min.get("amount") or 0) or float(cmp_max.get("amount") or 0)
    on_sale = compare_amt > price_amt > 0

    return {
        "id": node.get("id"),
        "handle": node.get("handle"),
        "title": node.get("title") or node.get("product", {}).get("title"),
        "vendor": node.get("vendor"),
        "product_type": node.get("productType"),
        "tags": node.get("tags") or [],
        "available_for_sale": node.get("availableForSale"),
        "image": (image or {}).get("url"),
        "price": _money(min_v.get("amount"), min_v.get("currencyCode")),
        "compare_at_price": _money(
            compare_amt if on_sale else None,
            cmp_min.get("currencyCode") or min_v.get("currencyCode"),
        ),
        "on_sale": on_sale,
        "currency_code": (min_v.get("currencyCode") or ""),
    }


def _extract_reviews(node: Dict[str, Any]) -> Dict[str, Any]:
    """Pull an aggregate ``{rating, review_count}`` from whichever review app's
    metafields are present. Returns zeros when the store has no review data."""
    import json as _json

    rating: Optional[float] = None
    count: int = 0
    by_key: Dict[str, Any] = {}
    for mf in node.get("metafields") or []:
        if mf and mf.get("value") is not None:
            by_key[f"{mf.get('namespace')}/{mf.get('key')}"] = mf.get("value")

    def _f(v: Any) -> Optional[float]:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # Shopify native rating metafield stores JSON: {"value": "4.4", ...}
    native = by_key.get("reviews/rating")
    if native:
        try:
            parsed = _json.loads(native) if isinstance(native, str) and native.strip().startswith("{") else None
            rating = _f(parsed.get("value")) if parsed else _f(native)
        except (ValueError, AttributeError):
            rating = _f(native)
    count = int(_f(by_key.get("reviews/rating_count")) or 0)

    # Loox
    if rating is None:
        rating = _f(by_key.get("loox/avg_rating"))
    if not count:
        count = int(_f(by_key.get("loox/num_reviews")) or 0)

    # Yotpo
    if rating is None:
        rating = _f(by_key.get("yotpo/reviews_average"))
    if not count:
        count = int(_f(by_key.get("yotpo/reviews_count")) or 0)

    # Okendo summary JSON
    okendo = by_key.get("okendo/summaryData")
    if okendo and (rating is None or not count):
        try:
            data = _json.loads(okendo) if isinstance(okendo, str) else okendo
            if rating is None:
                rating = _f(data.get("averageRating") or data.get("rating"))
            if not count:
                count = int(_f(data.get("reviewCount") or data.get("count")) or 0)
        except (ValueError, AttributeError):
            pass

    if rating is not None:
        rating = round(max(0.0, min(rating, 5.0)), 1)
    return {"rating": rating, "review_count": max(0, count)}


class StorefrontServiceError(RuntimeError):
    """Raised when the Storefront graph responds with errors / userErrors."""


class StorefrontService:
    """Server-side wrapper over the Shopify Storefront GraphQL API (2026-07)."""

    def __init__(
        self,
        store_domain: str,
        storefront_token: str,
        api_version: str = _DEFAULT_API_VERSION,
        timeout: float = 12.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.store_domain = (store_domain or "").strip().rstrip("/")
        self.storefront_token = storefront_token or ""
        self.api_version = api_version or _DEFAULT_API_VERSION
        self._url = f"https://{self.store_domain}/api/{self.api_version}/graphql.json"
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=5.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            follow_redirects=True,
            transport=transport,
        )

    async def close(self) -> None:
        await self._http.aclose()

    @property
    def is_configured(self) -> bool:
        return bool(self.store_domain and self.storefront_token)

    async def _graphql(
        self,
        query: str,
        variables: Optional[Dict[str, Any]] = None,
        *,
        buyer_ip: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self.is_configured:
            raise StorefrontServiceError("Storefront credentials are not configured")
        payload: Dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        headers = {
            "X-Shopify-Storefront-Access-Token": self.storefront_token,
            "Content-Type": "application/json",
        }
        if buyer_ip:
            headers["Shopify-Storefront-Buyer-IP"] = buyer_ip
        resp = await request_with_retries(
            lambda: self._http.post(self._url, json=payload, headers=headers),
            label="shopify-storefront-overlay",
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            raise StorefrontServiceError(f"Shopify Storefront error: {body['errors']}")
        return body.get("data", {}) or {}

    # ── Products ──────────────────────────────────────────────────────────────

    async def _run_search(
        self,
        query: str,
        first: int,
        sort_key: str,
        reverse: bool,
        buyer_ip: Optional[str],
    ) -> List[Dict[str, Any]]:
        """Single Storefront products(query:) call → normalized product list."""
        data = await self._graphql(
            _SEARCH_QUERY,
            {
                "query": query or "",
                "first": first,
                "sortKey": sort_key,
                "reverse": bool(reverse),
            },
            buyer_ip=buyer_ip,
        )
        edges = (data.get("products") or {}).get("edges") or []
        return [_normalize_product(_node(e)) for e in edges]

    async def search_products(
        self,
        query: str,
        first: int = 20,
        *,
        sort_key: Optional[str] = None,
        reverse: bool = False,
        buyer_ip: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Storefront search root — returns a result *envelope*, not a bare list.

        Natural-language price constraints ("under 5000", "between 1k and 3k")
        are parsed into ``variants.price`` filters so the keyword text still
        matches product titles. Because Shopify's search is fuzzy/OR-ranked we
        then post-filter multi-word queries so every qualifier ("formal") is
        honoured — we never dump unrelated products. The envelope:

            {
              "products": [...],          # normalized product dicts
              "count":     int,
              "query":     str,           # cleaned keyword text (no price NL)
              "raw_query": str,
              "match":     "exact" | "price_relaxed" | "none",
              "price":     {"min": float|None, "max": float|None},
              "message":   str,           # honest, speakable summary
            }
        """
        first_n = max(1, min(int(first or 20), 50))
        primary_sort = sort_key or "RELEVANCE"
        raw = query or ""
        sf_query, has_price = build_storefront_query(raw)
        parsed = parse_price_query(raw)
        # Lift size/variant selectors ("size 9", "uk 9", "XL") out of the keyword
        # text so they never become mandatory qualifiers — this is what kept the
        # grid blank for queries like "formal shoes size 9". The assistant's
        # search does the same, keeping the two result sets consistent.
        clean_terms = strip_option_terms(parsed["terms"])
        terms_display = clean_terms or raw.strip()
        sig = _significant_terms(clean_terms)
        lo, hi = parsed["min"], parsed["max"]
        # Only enforce qualifier matching for genuinely multi-word queries; a
        # single category word ("sneakers") trusts Shopify's relevance ranking.
        strict = len(sig) >= 2

        def _envelope(products: List[Dict[str, Any]], match: str) -> Dict[str, Any]:
            return {
                "products": products,
                "count": len(products),
                "query": terms_display,
                "raw_query": raw.strip(),
                "match": match,
                "price": {"min": lo, "max": hi},
                "message": self._search_message(match, terms_display, products, lo, hi),
            }

        # Tier 1 — keywords (+ price filter), then post-filter for qualifiers.
        raw_hits = await self._run_search(
            sf_query, first_n, primary_sort, reverse, buyer_ip
        )
        hits = [p for p in raw_hits if _within_price(p, lo, hi)]
        if strict:
            hits = [p for p in hits if _product_matches_terms(p, sig)]
        if hits:
            return _envelope(hits, "exact")

        # Tier 2 — keep the keywords, drop ONLY the price filter. Surfaces
        # products that exist but fall outside the budget, so we can be honest
        # ("no X under N, but here are our X") instead of returning nothing.
        if has_price and clean_terms:
            raw_relaxed = await self._run_search(
                clean_terms, first_n, "RELEVANCE", False, buyer_ip
            )
            relaxed = raw_relaxed
            if strict:
                relaxed = [p for p in raw_relaxed if _product_matches_terms(p, sig)]
            if relaxed:
                return _envelope(relaxed, "price_relaxed")

        # No honest match. Never dump unrelated best-sellers — return an empty
        # set with a clear message the overlay/voice can speak verbatim.
        return _envelope([], "none")

    @staticmethod
    def _search_message(
        match: str,
        terms: str,
        products: List[Dict[str, Any]],
        lo: Optional[float],
        hi: Optional[float],
    ) -> str:
        """Honest, speakable one-liner describing the search outcome."""
        label = (terms or "").strip() or "products"
        n = len(products)
        if match == "exact":
            if not (terms or "").strip():
                return "Here %s %d %s." % (
                    "is" if n == 1 else "are", n, "product" if n == 1 else "products",
                )
            return "Here %s %d %s for “%s”." % (
                "is" if n == 1 else "are", n, "match" if n == 1 else "matches", label,
            )
        if match == "price_relaxed":
            budget = ""
            if hi is not None:
                budget = " under %s" % _fmt_price(hi)
            elif lo is not None:
                budget = " over %s" % _fmt_price(lo)
            return (
                "I couldn't find %s%s, but here %s %d we do have."
                % (label, budget, "is" if n == 1 else "are", n)
            )
        return (
            "Sorry, I couldn't find any %s in the store right now. "
            "Want to try a different search?" % label
        )

    async def get_product(
        self, handle: str, *, buyer_ip: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Full product by handle, including variants + option matrix."""
        data = await self._graphql(
            _PRODUCT_QUERY, {"handle": handle or ""}, buyer_ip=buyer_ip
        )
        product = data.get("product")
        if not product:
            return None
        base = _normalize_product(product)
        variants = []
        for edge in (product.get("variants") or {}).get("edges") or []:
            variant = _node(edge)
            v_price = variant.get("price") or {}
            v_compare = variant.get("compareAtPrice") or {}
            v_img = variant.get("image") or {}
            compare_amt = float(v_compare.get("amount") or 0)
            price_amt = float(v_price.get("amount") or 0)
            variants.append({
                "id": variant.get("id"),
                "title": variant.get("title"),
                "sku": variant.get("sku"),
                "available_for_sale": variant.get("availableForSale"),
                "quantity_available": variant.get("quantityAvailable"),
                "image": v_img.get("url"),
                "price": _money(v_price.get("amount"), v_price.get("currencyCode")),
                "compare_at_price": _money(
                    compare_amt if compare_amt > price_amt else None,
                    v_compare.get("currencyCode") or v_price.get("currencyCode"),
                ),
                "on_sale": compare_amt > price_amt > 0,
                "selected_options": [
                    {"name": o.get("name"), "value": o.get("value")}
                    for o in (variant.get("selectedOptions") or [])
                ],
            })
        reviews = _extract_reviews(product)
        base.update({
            "description_html": product.get("descriptionHtml"),
            "description": product.get("description"),
            "rating": reviews["rating"],
            "review_count": reviews["review_count"],
            "options": [
                {"name": o.get("name"), "values": o.get("values") or []}
                for o in (product.get("options") or [])
            ],
            "images": [
                (i.get("node") or {}).get("url")
                for i in (product.get("images") or {}).get("edges") or []
                if (i.get("node") or {}).get("url")
            ],
            "variants": variants,
        })
        return base

    # ── Cart ──────────────────────────────────────────────────────────────────

    async def cart_create(
        self,
        *,
        lines: Optional[List[Dict[str, Any]]] = None,
        buyer_ip: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new Storefront cart. ``lines`` are ``{merchandiseId, quantity}``."""
        data = await self._graphql(
            _CART_CREATE_MUTATION,
            {"input": {"lines": lines or []}},
            buyer_ip=buyer_ip,
        )
        return self._handle_cart_result(data.get("cartCreate") or {})

    async def cart_get(
        self, cart_id: str, *, buyer_ip: Optional[str] = None
    ) -> Dict[str, Any]:
        data = await self._graphql(_CART_QUERY, {"cartId": cart_id}, buyer_ip=buyer_ip)
        cart = data.get("cart")
        if not cart:
            raise StorefrontServiceError("Cart not found")
        return self._normalize_cart(cart)

    async def cart_lines_add(
        self,
        cart_id: str,
        lines: List[Dict[str, Any]],
        *,
        buyer_ip: Optional[str] = None,
    ) -> Dict[str, Any]:
        data = await self._graphql(
            _CART_LINES_ADD_MUTATION,
            {"cartId": cart_id, "lines": lines},
            buyer_ip=buyer_ip,
        )
        return self._handle_cart_result(data.get("cartLinesAdd") or {})

    async def cart_discount_codes_update(
        self,
        cart_id: str,
        codes: List[str],
        *,
        buyer_ip: Optional[str] = None,
    ) -> Dict[str, Any]:
        data = await self._graphql(
            _CART_DISCOUNT_UPDATE_MUTATION,
            {"cartId": cart_id, "discountCodes": codes or []},
            buyer_ip=buyer_ip,
        )
        return self._handle_cart_result(data.get("cartDiscountCodesUpdate") or {})

    async def cart_buyer_identity_update(
        self,
        cart_id: str,
        buyer_identity: Dict[str, Any],
        *,
        buyer_ip: Optional[str] = None,
    ) -> Dict[str, Any]:
        data = await self._graphql(
            _CART_BUYER_UPDATE_MUTATION,
            {"cartId": cart_id, "buyerIdentity": buyer_identity},
            buyer_ip=buyer_ip,
        )
        return self._handle_cart_result(data.get("cartBuyerIdentityUpdate") or {})

    async def cart_delivery_addresses_replace(
        self,
        cart_id: str,
        addresses: List[Dict[str, Any]],
        *,
        buyer_ip: Optional[str] = None,
    ) -> Dict[str, Any]:
        data = await self._graphql(
            _CART_ADDRESS_REPLACE_MUTATION,
            {"cartId": cart_id, "deliveryAddresses": addresses},
            buyer_ip=buyer_ip,
        )
        return self._handle_cart_result(data.get("cartDeliveryAddressesReplace") or {})

    # ── Parsing helpers ───────────────────────────────────────────────────────

    def _handle_cart_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        user_errors = result.get("userErrors") or []
        cart = result.get("cart") or {}
        normalized = self._normalize_cart(cart) if cart else None
        if user_errors:
            return {"cart": normalized, "errors": user_errors}
        return normalized

    @staticmethod
    def _normalize_cart(cart: Dict[str, Any]) -> Dict[str, Any]:
        cost = cart.get("cost") or {}
        lines = []
        for edge in (cart.get("lines") or {}).get("edges") or []:
            line = _node(edge)
            merchandise = line.get("merchandise") or {}
            product = merchandise.get("product") or {}
            line_cost = (line.get("cost") or {}).get("totalAmount") or {}
            lines.append({
                "id": line.get("id"),
                "quantity": line.get("quantity", 0),
                "variant_id": merchandise.get("id"),
                "variant_title": merchandise.get("title"),
                "product_handle": product.get("handle"),
                "product_title": product.get("title"),
                "image": (product.get("featuredImage") or {}).get("url"),
                "unit_price": _money(
                    (merchandise.get("price") or {}).get("amount"),
                    (merchandise.get("price") or {}).get("currencyCode"),
                ),
                "line_total": _money(line_cost.get("amount"), line_cost.get("currencyCode")),
            })
        return {
            "cart_id": cart.get("id"),
            "checkout_url": cart.get("checkoutUrl"),
            "total_quantity": cart.get("totalQuantity", 0),
            "subtotal": _money(
                (cost.get("subtotalAmount") or {}).get("amount"),
                (cost.get("subtotalAmount") or {}).get("currencyCode"),
            ),
            "total": _money(
                (cost.get("totalAmount") or {}).get("amount"),
                (cost.get("totalAmount") or {}).get("currencyCode"),
            ),
            "discount_codes": [
                {"code": d.get("code"), "applicable": d.get("applicable")}
                for d in (cart.get("discountCodes") or [])
            ],
            "buyer_email": (cart.get("buyerIdentity") or {}).get("email"),
            "lines": lines,
        }