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
from typing import Any, Dict, List, Optional

import httpx

from ...core.http_retry import request_with_retries

logger = logging.getLogger(__name__)

_DEFAULT_API_VERSION = "2026-07"

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

    async def search_products(
        self,
        query: str,
        first: int = 20,
        *,
        sort_key: Optional[str] = None,
        reverse: bool = False,
        buyer_ip: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Storefront search root — relevance-ranked, first:N products."""
        data = await self._graphql(
            _SEARCH_QUERY,
            {
                "query": query or "",
                "first": max(1, min(int(first or 20), 50)),
                "sortKey": sort_key or "RELEVANCE",
                "reverse": bool(reverse),
            },
            buyer_ip=buyer_ip,
        )
        edges = (data.get("products") or {}).get("edges") or []
        return [_normalize_product(_node(e)) for e in edges]

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