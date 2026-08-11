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
      }
    }
  }
}
"""

_PRODUCT_QUERY = """
query StorefrontGetProduct($handle: String!) {
  product(handle: $handle) {
    id
    handle
    title
    descriptionHtml
    vendor
    productType
    tags
    availableForSale
    featuredImage { url altText }
    images(first: 10) { edges { node { url altText } } }
    priceRange {
      minVariantPrice { amount currencyCode }
      maxVariantPrice { amount currencyCode }
    }
    options { name values }
    variants(first: 250) {
      edges {
        node {
          id
          title
          availableForSale
          price { amount currencyCode }
          selectedOptions { name value }
        }
      }
    }
  }
}
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
    max_v = price_range.get("maxVariantPrice") or {}
    image = node.get("featuredImage") or (node.get("images") or {}).get("edges") or []
    if image and isinstance(image, list):
        image = image[0].get("node") or {}
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
        "compare_at_price": _money(max_v.get("amount"), max_v.get("currencyCode")),
        "currency_code": (min_v.get("currencyCode") or ""),
    }


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
            variants.append({
                "id": variant.get("id"),
                "title": variant.get("title"),
                "available_for_sale": variant.get("availableForSale"),
                "price": _money(
                    (variant.get("price") or {}).get("amount"),
                    (variant.get("price") or {}).get("currencyCode"),
                ),
                "selected_options": [
                    {"name": o.get("name"), "value": o.get("value")}
                    for o in (variant.get("selectedOptions") or [])
                ],
            })
        base.update({
            "description_html": product.get("descriptionHtml"),
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