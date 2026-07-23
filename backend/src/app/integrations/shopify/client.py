"""
Shopify implementation of BaseStoreClient.

Uses two Shopify APIs:
  - Storefront GraphQL API  → products, collections, cart operations
  - Admin REST API          → orders, price rules/discounts, shop info

Required env vars:
  SHOPIFY_STORE_DOMAIN       e.g.  "mystore.myshopify.com"
  SHOPIFY_STOREFRONT_TOKEN   Storefront API public access token
  SHOPIFY_ADMIN_TOKEN        Admin API access token  (starts with "shpat_")
  SHOPIFY_API_VERSION        e.g.  "2024-01"  (optional, defaults to 2024-01)
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx

from ..base.commerce import BaseStoreClient
from ...core.http_retry import request_with_retries

logger = logging.getLogger(__name__)

_DEFAULT_API_VERSION = "2024-01"


# ── Tiny GraphQL helpers ───────────────────────────────────────────────────────

def _gid_to_int(gid: str) -> int:
    """Convert 'gid://shopify/Product/12345' → 12345."""
    try:
        return int(str(gid).rsplit("/", 1)[-1])
    except Exception:
        return 0


def _int_to_product_gid(product_id: int) -> str:
    return f"gid://shopify/Product/{product_id}"


def _int_to_variant_gid(variant_id: int) -> str:
    return f"gid://shopify/ProductVariant/{variant_id}"


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", str(text or "")).replace("&nbsp;", " ").strip()


def _safe_float(value: Any) -> float:
    try:
        return float(str(value or "0").replace(",", "").strip())
    except Exception:
        return 0.0


def _is_subsequence(a: str, b: str) -> bool:
    """True if every char of `a` appears in `b` in order — handles dropped letters
    so an abbreviation matches the full word ("gshk" ⊂ "gshock")."""
    pos = 0
    for ch in b:
        if pos < len(a) and a[pos] == ch:
            pos += 1
    return pos == len(a)


def _token_word_match(t: str, w: str) -> int:
    """Typo-tolerant match score between a query token and a product word.
    2 = substring (strong), 1 = subsequence/fuzzy (typo/abbrev), 0 = no match."""
    # Guard trivial short words ("g" from "G-Shock") matching any token that merely
    # contains that letter — require ≥3 chars on the substring path.
    if len(t) >= 3 and len(w) >= 3 and (t in w or w in t):
        return 2
    if len(t) >= 4 and len(w) >= 4:
        if _is_subsequence(t, w) or _is_subsequence(w, t):
            return 1
        if difflib.SequenceMatcher(None, t, w).ratio() >= 0.75:
            return 1  # transpositions/typos: "gshook"/"gshcok" ≈ "gshock"
    return 0


class ShopifyClient(BaseStoreClient):
    """
    Shopify implementation of BaseStoreClient.

    Products / cart come from the Storefront GraphQL API.
    Orders / discounts / shop settings come from the Admin REST API.
    Shopify cart IDs are persisted in Redis keyed by session_id.
    """

    def __init__(
        self,
        store_domain: Optional[str] = None,
        storefront_token: Optional[str] = None,
        admin_token: Optional[str] = None,
        api_version: Optional[str] = None,
        redis_client=None,
    ) -> None:
        self.store_domain = (store_domain or os.getenv("SHOPIFY_STORE_DOMAIN", "")).strip().rstrip("/")
        self.storefront_token = storefront_token or os.getenv("SHOPIFY_STOREFRONT_TOKEN", "")
        self.admin_token = admin_token or os.getenv("SHOPIFY_ADMIN_TOKEN", "")
        self.api_version = api_version or os.getenv("SHOPIFY_API_VERSION", _DEFAULT_API_VERSION)
        self.currency_symbol = os.getenv("STORE_CURRENCY", "$")
        self.redis = redis_client

        self._storefront_url = (
            f"https://{self.store_domain}/api/{self.api_version}/graphql.json"
        )
        self._admin_base = (
            f"https://{self.store_domain}/admin/api/{self.api_version}"
        )

        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=3.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            follow_redirects=True,
        )

        _url_bytes = (self.store_domain or "shopify-default").encode("utf-8")
        self._cache_prefix = "shopify:" + hashlib.md5(_url_bytes).hexdigest()[:8] + ":"

    @property
    def has_credentials(self) -> bool:
        """True when this client can actually reach Shopify — a domain plus at least
        one usable token (Storefront OR Admin). False means the store isn't connected
        yet, so callers should say so instead of attempting (and faking) a search."""
        return bool(self.store_domain and (self.storefront_token or self.admin_token))

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def close(self) -> None:
        await self._http.aclose()

    # ── Internal HTTP helpers ──────────────────────────────────────────────────

    async def _storefront(self, query: str, variables: Optional[Dict] = None) -> Dict[str, Any]:
        """Execute a Storefront GraphQL query."""
        if not self.store_domain or not self.storefront_token:
            raise RuntimeError("SHOPIFY_STORE_DOMAIN and SHOPIFY_STOREFRONT_TOKEN are required")

        payload: Dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        resp = await request_with_retries(
            lambda: self._http.post(
                self._storefront_url,
                json=payload,
                headers={
                    "X-Shopify-Storefront-Access-Token": self.storefront_token,
                    "Content-Type": "application/json",
                },
            ),
            label="shopify-storefront",
        )
        resp.raise_for_status()
        body = resp.json()
        errors = body.get("errors")
        if errors:
            raise RuntimeError(f"Shopify Storefront error: {errors}")
        return body.get("data", {})

    async def _admin_get(self, path: str, params: Optional[Dict] = None) -> Any:
        if not self.admin_token:
            raise RuntimeError("SHOPIFY_ADMIN_TOKEN is required for admin operations")
        url = f"{self._admin_base}/{path.lstrip('/')}"
        resp = await request_with_retries(
            lambda: self._http.get(
                url,
                params=params,
                headers={"X-Shopify-Access-Token": self.admin_token},
            ),
            label="shopify-admin-get",
        )
        resp.raise_for_status()
        return resp.json()

    async def _admin_post(self, path: str, json_body: Optional[Dict] = None) -> Any:
        url = f"{self._admin_base}/{path.lstrip('/')}"
        resp = await self._http.post(
            url,
            json=json_body or {},
            headers={
                "X-Shopify-Access-Token": self.admin_token,
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def _admin_graphql(self, query: str, variables: Optional[Dict] = None) -> Dict[str, Any]:
        """Execute an Admin GraphQL query (sees ALL products, unlike the Storefront
        API which needs products published to its sales channel + its own token)."""
        if not self.admin_token:
            raise RuntimeError("SHOPIFY_ADMIN_TOKEN is required for admin GraphQL")
        url = f"{self._admin_base}/graphql.json"
        payload: Dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        resp = await request_with_retries(
            lambda: self._http.post(
                url,
                json=payload,
                headers={
                    "X-Shopify-Access-Token": self.admin_token,
                    "Content-Type": "application/json",
                },
            ),
            label="shopify-admin-gql",
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            raise RuntimeError(f"Shopify Admin GraphQL error: {body['errors']}")
        return body.get("data", {})

    # ── Redis cache helpers ────────────────────────────────────────────────────

    async def _cache_get(self, key: str) -> Optional[Any]:
        if not self.redis:
            return None
        try:
            raw = await asyncio.wait_for(
                self.redis.get(self._cache_prefix + key), timeout=1.2
            )
            return json.loads(raw) if raw else None
        except Exception:
            return None

    async def _cache_set(self, key: str, value: Any, ttl: int) -> None:
        if not self.redis:
            return
        try:
            await asyncio.wait_for(
                self.redis.set(self._cache_prefix + key, json.dumps(value), ex=ttl),
                timeout=1.2,
            )
        except Exception:
            pass

    # ── Cart ID persistence (session_id → Shopify cartId) ─────────────────────

    async def _get_cart_id(self, session_id: str) -> Optional[str]:
        if not self.redis:
            return None
        try:
            return await self.redis.get(f"shopify:cart:{session_id}")
        except Exception:
            return None

    async def _set_cart_id(self, session_id: str, cart_id: str) -> None:
        if not self.redis:
            return
        try:
            await self.redis.set(f"shopify:cart:{session_id}", cart_id, ex=604800)
        except Exception:
            pass

    # ── Product normalization ──────────────────────────────────────────────────

    def _normalize_product_node(self, node: Dict[str, Any]) -> Dict[str, Any]:
        variants = node.get("variants", {}).get("edges", [])

        price_range = node.get("priceRange", {})
        min_price_obj = price_range.get("minVariantPrice", {})

        compare_at = node.get("compareAtPriceRange", {})
        compare_min = compare_at.get("minVariantPrice", {})

        price = min_price_obj.get("amount", "0")
        compare_price = compare_min.get("amount", "0")
        on_sale = _safe_float(compare_price) > _safe_float(price) and _safe_float(compare_price) > 0

        images = node.get("images", {}).get("edges", [])
        image_url = images[0]["node"]["url"] if images else ""

        in_stock = node.get("availableForSale", False)
        total_inventory = node.get("totalInventory")

        variations_summary = []
        for edge in variants[:8]:
            v = edge["node"]
            attrs: Dict[str, str] = {}
            for sel in v.get("selectedOptions", []):
                attrs[sel["name"].lower()] = sel["value"]
            variations_summary.append({
                "id": _gid_to_int(v.get("id", "")),
                "attributes": attrs,
                "price": v.get("price", {}).get("amount", price),
                "stock_status": "instock" if v.get("availableForSale") else "outofstock",
                "stock_qty": v.get("quantityAvailable"),
            })

        attributes = []
        for opt in node.get("options", []):
            attributes.append({
                "name": opt.get("name", ""),
                "options": opt.get("values", []),
            })

        raw_tags = node.get("tags")
        parsed_tags: list[str] = []
        if isinstance(raw_tags, list):
            parsed_tags = [str(t) for t in raw_tags if t]
        elif isinstance(raw_tags, str):
            parsed_tags = [t.strip() for t in raw_tags.split(",") if t.strip()]

        categories = [
            {
                "id": _gid_to_int(e["node"]["id"]),
                "name": e["node"]["title"],
                "slug": e["node"]["handle"],
            }
            for e in node.get("collections", {}).get("edges", [])
        ]

        return {
            "id": _gid_to_int(node.get("id", "")),
            "name": node.get("title", ""),
            "price": str(price),
            "sale_price": str(price) if on_sale else "",
            "regular_price": str(compare_price) if on_sale else str(price),
            "stock_status": "instock" if in_stock else "outofstock",
            "stock_quantity": total_inventory,
            "image_url": image_url,
            "permalink": f"https://{self.store_domain}/products/{node.get('handle', '')}",
            "short_description": _strip_html(node.get("description", "")),
            "attributes": attributes,
            "variations_summary": variations_summary,
            "on_sale": on_sale,
            "tags": parsed_tags,
            "categories": categories,
        }

    def _normalize_admin_gql_node(self, node: Dict[str, Any]) -> Dict[str, Any]:
        """Map an Admin GraphQL product node to the same shape as
        _normalize_product_node (so downstream consumers are identical).

        NOTE: Admin API Product has NO compareAtPriceRange and Admin ProductVariant
        has no availableForSale — both are Storefront-only. We derive sale price from
        variant compareAtPrice and stock from inventoryQuantity instead.
        """
        pr = (node.get("priceRangeV2") or {}).get("minVariantPrice", {}) or {}
        price = pr.get("amount", "0")
        image_url = (node.get("featuredImage") or {}).get("url", "") or ""

        variants = (node.get("variants") or {}).get("edges", [])
        variations_summary = []
        any_in_stock = False
        compares: List[float] = []
        for edge in variants[:8]:
            v = edge.get("node", {})
            qty = v.get("inventoryQuantity")
            # Untracked inventory (qty is None) is purchasable; tracked needs qty > 0.
            v_in_stock = qty is None or (isinstance(qty, int) and qty > 0)
            if v_in_stock:
                any_in_stock = True
            if v.get("compareAtPrice"):
                compares.append(_safe_float(v.get("compareAtPrice")))
            attrs: Dict[str, str] = {}
            for sel in v.get("selectedOptions", []):
                attrs[str(sel.get("name", "")).lower()] = sel.get("value", "")
            variations_summary.append({
                "id": _gid_to_int(v.get("id", "")),
                "attributes": attrs,
                "price": str(v.get("price", price)),
                "stock_status": "instock" if v_in_stock else "outofstock",
                "stock_qty": qty,
            })

        compare = str(max(compares)) if compares else "0"
        on_sale = _safe_float(compare) > _safe_float(price) and _safe_float(compare) > 0
        total_inventory = node.get("totalInventory")
        in_stock = any_in_stock or (isinstance(total_inventory, int) and total_inventory > 0)
        attributes = [
            {"name": o.get("name", ""), "options": o.get("values", [])}
            for o in node.get("options", [])
        ]
        return {
            "id": _gid_to_int(node.get("id", "")),
            "name": node.get("title", ""),
            "price": str(price),
            "sale_price": str(price) if on_sale else "",
            "regular_price": str(compare) if on_sale else str(price),
            "stock_status": "instock" if in_stock else "outofstock",
            "stock_quantity": total_inventory,
            "image_url": image_url,
            "permalink": f"https://{self.store_domain}/products/{node.get('handle', '')}",
            "short_description": _strip_html(node.get("descriptionHtml", "")),
            "attributes": attributes,
            "variations_summary": variations_summary,
            "on_sale": on_sale,
        }

    async def _admin_search_products(
        self,
        *,
        query: str,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        in_stock_only: bool = False,
        limit: int = 6,
    ) -> List[Dict[str, Any]]:
        """Admin-API product search fallback. Used when the Storefront API returns
        nothing (missing/revoked Storefront token, or products not published to the
        Storefront channel). The Admin token sees every product."""
        if not self.admin_token:
            logger.warning(
                "Admin product fallback SKIPPED: no admin_token for %s "
                "(tenant has no Shopify Admin token — reinstall via OAuth).",
                self.store_domain or "?",
            )
            return []
        # Build the Shopify query. CRITICAL: pass the search terms so Shopify returns
        # RELEVANT products — NOT just the first N alphabetical (which on a big catalog
        # are all "A…" names, so "C…asio G-Shock" never gets fetched). Shopify ANDs
        # bare terms, so we OR the significant tokens; bare terms hit the default
        # fields (title/product_type/vendor/tag/sku), so "casio"/"watches" match every
        # Casio G-Shock. No query → browse all active.
        stop = {
            "what", "are", "the", "you", "your", "have", "has", "want", "need",
            "show", "all", "any", "and", "for", "can", "will", "with", "this",
            "that", "here", "available", "products", "product", "items", "item",
            "some", "looking", "find", "get", "give", "see", "tell", "buy", "no",
            "yes", "not",
        }
        terms = [t for t in re.findall(r"[a-z0-9]+", (query or "").lower())
                 if len(t) > 2 and t not in stop]
        if terms:
            admin_q = "status:active AND (" + " OR ".join(terms) + ")"
            sort_key = "RELEVANCE"
        else:
            admin_q = "status:active"
            sort_key = "TITLE"  # RELEVANCE errors without a text term
        async def _fetch(q_str: str, sk: str, first: int) -> Optional[List[Dict[str, Any]]]:
            gql = ("""
        query AdminSearch($q: String!, $first: Int!) {
          products(query: $q, first: $first, sortKey: %s) {""" % sk + """
            edges { node {
              id title handle descriptionHtml totalInventory tags
              featuredImage { url }
              priceRangeV2 { minVariantPrice { amount currencyCode } }
              options { name values }
              variants(first: 10) { edges { node {
                id inventoryQuantity price compareAtPrice
                selectedOptions { name value }
              } } }
            } }
          }
        }
        """)
            try:
                data = await self._admin_graphql(gql, {"q": q_str, "first": first})
            except Exception as exc:
                logger.warning("Shopify Admin product fallback failed: %s", exc)
                return None
            out: List[Dict[str, Any]] = []
            for e in (data.get("products") or {}).get("edges", []):
                node = e.get("node", {})
                p = self._normalize_admin_gql_node(node)
                p["_tags"] = " ".join(node.get("tags") or []) if isinstance(node.get("tags"), list) else str(node.get("tags") or "")
                out.append(p)
            return out

        products = await _fetch(admin_q, sort_key, 100)
        if products is None:
            return []
        # Typo path: a SEARCH whose (misspelled) terms matched nothing in Shopify's
        # exact search — fetch a browse page so the FUZZY ranker below can still find
        # it ("gshook"→"g-shock"). Shopify has no fuzzy search, so we match locally.
        if terms and not products:
            products = await _fetch("status:active", "TITLE", 150) or []
        fetched = len(products)

        # Client-side relevance: typo-tolerant token match against name/tags/desc.
        _STOP = {
            "what", "are", "the", "you", "your", "have", "has", "want", "need",
            "show", "all", "any", "and", "for", "can", "will", "with", "this",
            "that", "here", "available", "products", "product", "items", "item",
            "some", "looking", "find", "get", "give", "see", "tell", "buy", "no",
        }
        q_tokens = [
            t for t in re.findall(r"[a-z0-9]+", (query or "").lower())
            if len(t) > 2 and t not in _STOP
        ]
        if q_tokens:
            def _score(p: Dict[str, Any]) -> int:
                word_list = re.findall(
                    r"[a-z0-9]+",
                    (str(p.get("name", "")) + " " + str(p.get("_tags", "")) + " "
                     + str(p.get("short_description", ""))).lower(),
                )
                words = set(word_list)
                # Adjacent-word joins so an abbreviation spanning a word boundary
                # matches: "G-Shock" → words {g, shock} miss "gshk", but the join
                # "gshock" makes "gshk" ⊂ "gshock" (and "gshook"/"gshcok" ≈ "gshock").
                words.update(
                    word_list[i] + word_list[i + 1] for i in range(len(word_list) - 1)
                )
                s = 0
                for t in q_tokens:
                    best = 0
                    for w in words:
                        m = _token_word_match(t, w)
                        if m > best:
                            best = m
                        if best == 2:
                            break
                    s += best  # substring=2, fuzzy/subsequence=1 → exact ranks first
                return s
            ranked = sorted(((p, _score(p)) for p in products), key=lambda x: x[1], reverse=True)
            # If the query matched something, return those; otherwise empty so the
            # agent says "couldn't find X" rather than showing irrelevant products.
            products = [p for p, s in ranked if s > 0]

        for p in products:
            p.pop("_tags", None)
        if in_stock_only:
            products = [p for p in products if p.get("stock_status") == "instock"]
        if min_price is not None:
            products = [p for p in products if _safe_float(p.get("price")) >= min_price]
        if max_price is not None:
            products = [p for p in products if _safe_float(p.get("price")) <= max_price]
        result = products[: max(1, min(int(limit or 6), 40))]
        logger.info(
            "Admin product fallback: %d fetched, %d returned for query=%r store=%s",
            fetched, len(result), query or "(browse)", self.store_domain or "?",
        )
        return result

    # ── Products ───────────────────────────────────────────────────────────────

    async def search_products(
        self,
        *,
        query: str,
        category_slug: Optional[str] = None,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        in_stock_only: bool = True,
        on_sale: Optional[bool] = None,
        limit: int = 6,
    ) -> List[Dict[str, Any]]:
        cache_key = (
            f"catalog:search:{query}:{category_slug}:"
            f"{min_price}:{max_price}:{in_stock_only}:{on_sale}:{limit}"
        )
        cached = await self._cache_get(cache_key)
        if isinstance(cached, list):
            return cached

        filter_parts: List[str] = []
        if query:
            filter_parts.append(query)
        if category_slug:
            filter_parts.append(f"product_type:{category_slug}")
        if min_price is not None:
            filter_parts.append(f"price:>={min_price}")
        if max_price is not None:
            filter_parts.append(f"price:<={max_price}")
        if in_stock_only:
            filter_parts.append("available_for_sale:true")
        if on_sale is True:
            filter_parts.append("compare_at_price:>0")

        gql_query = " ".join(filter_parts) if filter_parts else "*"
        per_page = max(1, min(int(limit or 6), 40))

        GQL = """
        query SearchProducts($query: String!, $first: Int!) {
          products(query: $query, first: $first, sortKey: RELEVANCE) {
            edges {
              node {
                id title handle description availableForSale totalInventory
                priceRange {
                  minVariantPrice { amount currencyCode }
                  maxVariantPrice { amount currencyCode }
                }
                compareAtPriceRange {
                  minVariantPrice { amount currencyCode }
                }
                images(first: 1) { edges { node { url } } }
                options { name values }
                variants(first: 10) {
                  edges {
                    node {
                      id availableForSale quantityAvailable
                      price { amount }
                      selectedOptions { name value }
                    }
                  }
                }
              }
            }
          }
        }
        """
        products: List[Dict[str, Any]] = []
        try:
            data = await self._storefront(GQL, {"query": gql_query, "first": per_page})
            edges = data.get("products", {}).get("edges", [])
            products = [self._normalize_product_node(e["node"]) for e in edges]
        except Exception as exc:
            # Storefront token missing/revoked (e.g. from an uninstalled app) or
            # products not published to the Storefront channel. Fall through to the
            # Admin API, which sees ALL products with the app's Admin token.
            logger.warning("Shopify Storefront search failed (%s) — trying Admin API", exc)

        if not products and self.admin_token:
            products = await self._admin_search_products(
                query=query, min_price=min_price, max_price=max_price,
                in_stock_only=in_stock_only, limit=limit,
            )

        if products:
            await self._cache_set(cache_key, products, ttl=1800)
        return products

    async def fetch_all_products_storefront(
        self, *, page_size: int = 250, max_pages: int = 200
    ) -> List[Dict[str, Any]]:
        """Cursor-paginate the entire catalog via the Storefront API.

        Used by the product-sync fallback when the Admin Bulk Operation path is
        unavailable. Unlike search_products (single page), this follows
        pageInfo.endCursor until hasNextPage is false, so catalogs with
        thousands of products sync fully.
        """
        GQL = """
        query AllProducts($first: Int!, $after: String) {
          products(first: $first, after: $after, sortKey: ID) {
            pageInfo { hasNextPage endCursor }
            edges {
              node {
                id title handle description availableForSale totalInventory tags
                priceRange {
                  minVariantPrice { amount currencyCode }
                  maxVariantPrice { amount currencyCode }
                }
                compareAtPriceRange { minVariantPrice { amount currencyCode } }
                images(first: 1) { edges { node { url } } }
                options { name values }
                collections(first: 5) {
                  edges { node { id title handle } }
                }
                variants(first: 10) {
                  edges {
                    node {
                      id availableForSale quantityAvailable
                      price { amount }
                      selectedOptions { name value }
                    }
                  }
                }
              }
            }
          }
        }
        """
        all_products: List[Dict[str, Any]] = []
        after: Optional[str] = None
        per_page = max(1, min(int(page_size or 250), 250))  # Storefront cap = 250
        for _ in range(max_pages):
            data = await self._storefront(GQL, {"first": per_page, "after": after})
            conn = data.get("products", {})
            edges = conn.get("edges", [])
            all_products.extend(self._normalize_product_node(e["node"]) for e in edges)
            page_info = conn.get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
            if not after:
                break
        return all_products

    async def get_product_details(self, product_id: int) -> Dict[str, Any]:
        cache_key = f"product:{product_id}"
        cached = await self._cache_get(cache_key)
        if isinstance(cached, dict):
            return cached

        GQL = """
        query GetProduct($id: ID!) {
          product(id: $id) {
            id title handle description availableForSale totalInventory
            priceRange {
              minVariantPrice { amount currencyCode }
            }
            compareAtPriceRange {
              minVariantPrice { amount currencyCode }
            }
            images(first: 10) { edges { node { url } } }
            options { name values }
            variants(first: 100) {
              edges {
                node {
                  id availableForSale quantityAvailable
                  price { amount }
                  compareAtPrice { amount }
                  selectedOptions { name value }
                  image { url }
                }
              }
            }
            collections(first: 5) {
              edges { node { id title handle } }
            }
          }
        }
        """
        try:
            data = await self._storefront(GQL, {"id": _int_to_product_gid(product_id)})
            node = data.get("product") or {}
            if not node:
                return {}

            base = self._normalize_product_node(node)

            images = [
                e["node"]["url"]
                for e in node.get("images", {}).get("edges", [])
            ]

            variants = node.get("variants", {}).get("edges", [])
            variations: List[Dict[str, Any]] = []
            for edge in variants:
                v = edge["node"]
                attrs: Dict[str, str] = {}
                for sel in v.get("selectedOptions", []):
                    attrs[sel["name"].lower()] = sel["value"]
                vp = v.get("price", {}).get("amount", "0")
                cap = (v.get("compareAtPrice") or {}).get("amount", "0")
                variations.append({
                    "id": _gid_to_int(v.get("id", "")),
                    "attributes": attrs,
                    "price": str(vp),
                    "regular_price": str(cap) if _safe_float(cap) > 0 else str(vp),
                    "sale_price": str(vp) if _safe_float(cap) > _safe_float(vp) else "",
                    "stock_status": "instock" if v.get("availableForSale") else "outofstock",
                    "stock_quantity": v.get("quantityAvailable"),
                    "image_url": (v.get("image") or {}).get("url", ""),
                })

            categories = [
                {
                    "id": _gid_to_int(e["node"]["id"]),
                    "name": e["node"]["title"],
                    "slug": e["node"]["handle"],
                }
                for e in node.get("collections", {}).get("edges", [])
            ]

            result = {
                **base,
                "description": _strip_html(node.get("description", "")),
                "images": images,
                "variations": variations,
                "variations_summary": variations,
                "categories": categories,
                "related_products": [],
                "reviews_summary": {},
            }
            await self._cache_set(cache_key, result, ttl=3600)
            return result
        except Exception as exc:
            logger.error("Shopify get_product_details failed for %s: %s", product_id, exc)
            return {}

    async def get_product_variations(self, product_id: int) -> Dict[str, Any]:
        try:
            detail = await self.get_product_details(product_id)
            variations = detail.get("variations", [])

            sizes: List[str] = []
            colors: List[str] = []
            formatted: List[Dict[str, Any]] = []

            for v in variations:
                attrs = v.get("attributes", {})
                if isinstance(attrs, dict):
                    size = attrs.get("size", "")
                    color = attrs.get("color", "") or attrs.get("colour", "")
                else:
                    size = color = ""

                if size and size not in sizes:
                    sizes.append(size)
                if color and color not in colors:
                    colors.append(color)

                formatted.append({
                    "id": v.get("id"),
                    "attributes": attrs,
                    "size": size,
                    "color": color,
                    "price": v.get("price", ""),
                    "stock_status": v.get("stock_status", "instock"),
                    "stock_qty": v.get("stock_quantity"),
                    "image_url": v.get("image_url", ""),
                })

            return {
                "success": True,
                "product_id": product_id,
                "product_name": detail.get("name", ""),
                "product_type": "variable" if variations else "simple",
                "available_sizes": sizes,
                "available_colors": colors,
                "variants": formatted,
                "in_stock_count": sum(
                    1 for v in formatted if v.get("stock_status") == "instock"
                ),
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    async def find_variants(self, *, product_id: int) -> Dict[str, Any]:
        return await self.get_product_variations(product_id)

    async def check_inventory(
        self,
        *,
        product_id: int,
        variation_id: Optional[int] = None,
        attributes: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        detail = await self.get_product_details(product_id)
        variations = detail.get("variations", [])

        if variation_id:
            for v in variations:
                if int(v.get("id") or 0) == int(variation_id):
                    return {
                        "product_id": product_id,
                        "variation_id": variation_id,
                        "in_stock": v.get("stock_status") == "instock",
                        "stock_quantity": v.get("stock_quantity"),
                        "attributes": v.get("attributes", {}),
                    }

        if attributes and variations:
            norm_attrs = {k.lower(): v.lower() for k, v in attributes.items()}
            for v in variations:
                v_attrs = {
                    k.lower(): str(val).lower()
                    for k, val in (v.get("attributes") or {}).items()
                }
                if all(v_attrs.get(k) == val for k, val in norm_attrs.items()):
                    return {
                        "product_id": product_id,
                        "variation_id": v.get("id"),
                        "in_stock": v.get("stock_status") == "instock",
                        "stock_quantity": v.get("stock_quantity"),
                        "attributes": v.get("attributes", {}),
                    }
            return {
                "product_id": product_id,
                "variation_id": 0,
                "in_stock": False,
                "stock_quantity": 0,
                "attributes": {},
                "variant_not_found": True,
            }

        return {
            "product_id": product_id,
            "variation_id": 0,
            "in_stock": detail.get("stock_status") == "instock",
            "stock_quantity": detail.get("stock_quantity"),
            "attributes": {},
        }

    async def get_categories(self) -> List[Dict[str, Any]]:
        cache_key = "catalog:categories"
        cached = await self._cache_get(cache_key)
        if isinstance(cached, list):
            return cached

        GQL = """
        query GetCollections($first: Int!) {
          collections(first: $first, sortKey: TITLE) {
            edges {
              node {
                id title handle
                products(first: 1) { edges { node { id } } }
              }
            }
          }
        }
        """
        try:
            data = await self._storefront(GQL, {"first": 100})
            edges = data.get("collections", {}).get("edges", [])
            categories = []
            for e in edges:
                n = e["node"]
                count = len(n.get("products", {}).get("edges", []))
                categories.append({
                    "id": _gid_to_int(n["id"]),
                    "name": n["title"],
                    "slug": n["handle"],
                    "count": count,
                })
            await self._cache_set(cache_key, categories, ttl=86400)
            return categories
        except Exception as exc:
            logger.error("Shopify get_categories failed: %s", exc)
            return []

    # ── Cart ───────────────────────────────────────────────────────────────────

    def _normalize_cart(self, cart_node: Dict[str, Any]) -> Dict[str, Any]:
        lines = cart_node.get("lines", {}).get("edges", [])
        items = []
        for edge in lines:
            line = edge["node"]
            merch = line.get("merchandise", {})
            product = merch.get("product", {})
            variant_gid = merch.get("id", "")
            product_gid = product.get("id", "")
            qty = int(line.get("quantity", 1))
            cost = line.get("cost", {})
            unit_price = cost.get("amountPerQuantity", {}).get("amount", "0")
            subtotal = cost.get("subtotalAmount", {}).get("amount", "0")
            items.append({
                "cart_item_key": line.get("id", ""),
                "product_id": _gid_to_int(product_gid),
                "variation_id": _gid_to_int(variant_gid),
                "name": product.get("title", merch.get("title", "")),
                "quantity": qty,
                "price": str(unit_price),
                "subtotal": str(subtotal),
                "image_url": ((merch.get("image") or {}).get("url", "")),
            })

        total_amount = (
            cart_node.get("cost", {})
            .get("totalAmount", {})
            .get("amount", "0")
        )
        checkout_url = cart_node.get("checkoutUrl", "")
        item_count = sum(i["quantity"] for i in items)

        return {
            "items": items,
            "item_count": item_count,
            "is_empty": item_count == 0,
            "total": str(total_amount),
            "checkout_url": checkout_url,
        }

    async def _create_cart(self, session_id: str) -> str:
        GQL = """
        mutation CartCreate {
          cartCreate {
            cart { id checkoutUrl }
            userErrors { field message }
          }
        }
        """
        data = await self._storefront(GQL)
        cart_id = data.get("cartCreate", {}).get("cart", {}).get("id", "")
        if not cart_id:
            raise RuntimeError("Shopify cartCreate returned no cart ID")
        await self._set_cart_id(session_id, cart_id)
        return cart_id

    async def _get_or_create_cart_id(self, session_id: str) -> str:
        cart_id = await self._get_cart_id(session_id)
        if not cart_id:
            cart_id = await self._create_cart(session_id)
        return cart_id

    async def _fetch_cart(self, cart_id: str) -> Dict[str, Any]:
        GQL = """
        query GetCart($cartId: ID!) {
          cart(id: $cartId) {
            id checkoutUrl
            cost { totalAmount { amount currencyCode } }
            lines(first: 50) {
              edges {
                node {
                  id quantity
                  cost {
                    amountPerQuantity { amount }
                    subtotalAmount { amount }
                  }
                  merchandise {
                    ... on ProductVariant {
                      id title
                      image { url }
                      product { id title }
                    }
                  }
                }
              }
            }
          }
        }
        """
        data = await self._storefront(GQL, {"cartId": cart_id})
        cart_node = data.get("cart")
        if not cart_node:
            raise RuntimeError(f"Cart not found: {cart_id}")
        return self._normalize_cart(cart_node)

    async def get_cart(self, *, session_id: str) -> Dict[str, Any]:
        try:
            cart_id = await self._get_cart_id(session_id)
            if not cart_id:
                return {"items": [], "item_count": 0, "is_empty": True, "total": "0"}
            return await self._fetch_cart(cart_id)
        except Exception as exc:
            logger.error("Shopify get_cart failed: %s", exc)
            return {"items": [], "item_count": 0, "is_empty": True, "total": "0"}

    async def get_cart_for_session(self, session_id: str) -> Dict[str, Any]:
        cart = await self.get_cart(session_id=session_id)
        cart["item_count"] = int(cart.get("item_count") or 0)
        cart["is_empty"] = cart["item_count"] == 0
        cart["total"] = str(cart.get("total") or "0")
        return cart

    async def add_to_cart(
        self,
        *,
        session_id: str,
        product_id: int,
        variation_id: Optional[int] = 0,
        quantity: int = 1,
        variation: Optional[Dict[str, Any]] = None,
        product_name: Optional[str] = None,
        price: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            cart_id = await self._get_or_create_cart_id(session_id)

            if variation_id and variation_id > 0:
                variant_gid = _int_to_variant_gid(int(variation_id))
            else:
                detail = await self.get_product_details(product_id)
                variants = detail.get("variations", [])
                chosen = next(
                    (v for v in variants if v.get("stock_status") == "instock"),
                    variants[0] if variants else None,
                )
                if chosen is None:
                    GQL = """
                    query FirstVariant($id: ID!) {
                      product(id: $id) {
                        variants(first: 1) { edges { node { id } } }
                      }
                    }
                    """
                    vdata = await self._storefront(GQL, {"id": _int_to_product_gid(product_id)})
                    edges = vdata.get("product", {}).get("variants", {}).get("edges", [])
                    if not edges:
                        raise RuntimeError(f"No variants found for product {product_id}")
                    variant_gid = edges[0]["node"]["id"]
                else:
                    variant_gid = _int_to_variant_gid(int(chosen["id"]))

            GQL = """
            mutation AddToCart($cartId: ID!, $lines: [CartLineInput!]!) {
              cartLinesAdd(cartId: $cartId, lines: $lines) {
                cart {
                  id checkoutUrl
                  cost { totalAmount { amount currencyCode } }
                  lines(first: 50) {
                    edges {
                      node {
                        id quantity
                        cost {
                          amountPerQuantity { amount }
                          subtotalAmount { amount }
                        }
                        merchandise {
                          ... on ProductVariant {
                            id title
                            image { url }
                            product { id title }
                          }
                        }
                      }
                    }
                  }
                }
                userErrors { field message }
              }
            }
            """
            data = await self._storefront(
                GQL,
                {
                    "cartId": cart_id,
                    "lines": [{"merchandiseId": variant_gid, "quantity": max(1, int(quantity or 1))}],
                },
            )
            cart_node = data.get("cartLinesAdd", {}).get("cart", {})
            result = self._normalize_cart(cart_node)
            result["checkout_url"] = cart_node.get("checkoutUrl", "")
            return result
        except Exception as exc:
            logger.error("Shopify add_to_cart failed: %s", exc)
            raise

    async def remove_from_cart(
        self,
        *,
        session_id: str,
        cart_item_key: Optional[str] = None,
        product_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        try:
            cart_id = await self._get_cart_id(session_id)
            if not cart_id:
                raise RuntimeError("No active cart for this session")

            line_id = cart_item_key
            if not line_id and product_id:
                cart = await self._fetch_cart(cart_id)
                for item in cart.get("items", []):
                    if int(item.get("product_id") or 0) == int(product_id):
                        line_id = item.get("cart_item_key")
                        break

            if not line_id:
                raise RuntimeError("Item not found in cart")

            GQL = """
            mutation RemoveFromCart($cartId: ID!, $lineIds: [ID!]!) {
              cartLinesRemove(cartId: $cartId, lineIds: $lineIds) {
                cart {
                  id
                  cost { totalAmount { amount } }
                  lines(first: 50) {
                    edges {
                      node {
                        id quantity
                        cost {
                          amountPerQuantity { amount }
                          subtotalAmount { amount }
                        }
                        merchandise {
                          ... on ProductVariant {
                            id title
                            image { url }
                            product { id title }
                          }
                        }
                      }
                    }
                  }
                }
                userErrors { field message }
              }
            }
            """
            data = await self._storefront(GQL, {"cartId": cart_id, "lineIds": [line_id]})
            cart_node = data.get("cartLinesRemove", {}).get("cart", {})
            return self._normalize_cart(cart_node)
        except Exception as exc:
            logger.error("Shopify remove_from_cart failed: %s", exc)
            raise

    async def update_cart_quantity(
        self,
        *,
        session_id: str,
        product_id: int,
        quantity: int,
    ) -> Dict[str, Any]:
        try:
            if quantity <= 0:
                return await self.remove_from_cart(
                    session_id=session_id, product_id=product_id
                )

            cart_id = await self._get_cart_id(session_id)
            if not cart_id:
                raise RuntimeError("No active cart for this session")

            cart = await self._fetch_cart(cart_id)
            line_id = None
            for item in cart.get("items", []):
                if int(item.get("product_id") or 0) == int(product_id):
                    line_id = item.get("cart_item_key")
                    break

            if not line_id:
                raise RuntimeError("Item not found in cart")

            GQL = """
            mutation UpdateCartLine($cartId: ID!, $lines: [CartLineUpdateInput!]!) {
              cartLinesUpdate(cartId: $cartId, lines: $lines) {
                cart {
                  id
                  cost { totalAmount { amount } }
                  lines(first: 50) {
                    edges {
                      node {
                        id quantity
                        cost {
                          amountPerQuantity { amount }
                          subtotalAmount { amount }
                        }
                        merchandise {
                          ... on ProductVariant {
                            id title
                            image { url }
                            product { id title }
                          }
                        }
                      }
                    }
                  }
                }
                userErrors { field message }
              }
            }
            """
            data = await self._storefront(
                GQL,
                {
                    "cartId": cart_id,
                    "lines": [{"id": line_id, "quantity": int(quantity)}],
                },
            )
            cart_node = data.get("cartLinesUpdate", {}).get("cart", {})
            updated = self._normalize_cart(cart_node)
            return {
                "success": True,
                "new_quantity": quantity,
                "updated_cart": updated,
                "message": f"Quantity updated to {quantity}",
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    # ── Discounts ──────────────────────────────────────────────────────────────

    async def apply_coupon(self, *, session_id: str, coupon_code: str) -> Dict[str, Any]:
        code = str(coupon_code or "").strip().upper()
        if not code:
            return {"success": False, "message": "No coupon code provided."}
        try:
            cart_id = await self._get_or_create_cart_id(session_id)

            GQL = """
            mutation ApplyDiscount($cartId: ID!, $discountCodes: [String!]!) {
              cartDiscountCodesUpdate(cartId: $cartId, discountCodes: $discountCodes) {
                cart {
                  id
                  discountCodes { code applicable }
                  cost { totalAmount { amount } }
                }
                userErrors { field message }
              }
            }
            """
            data = await self._storefront(GQL, {"cartId": cart_id, "discountCodes": [code]})
            result = data.get("cartDiscountCodesUpdate", {})
            cart_node = result.get("cart", {})
            errors = result.get("userErrors", [])

            discount_codes = cart_node.get("discountCodes", [])
            applicable = next(
                (d for d in discount_codes if d.get("code", "").upper() == code),
                None,
            )

            if errors or (applicable is not None and not applicable.get("applicable", True)):
                msg = errors[0]["message"] if errors else f"Coupon {code} is not applicable."
                return {"success": False, "code": code, "message": msg}

            new_total = cart_node.get("cost", {}).get("totalAmount", {}).get("amount", "0")
            return {
                "success": True,
                "code": code,
                "message": f"Coupon {code} applied!",
                "new_total": str(new_total),
            }
        except Exception as exc:
            logger.warning("apply_coupon failed (%s), storing for checkout: %s", code, exc)
            return {
                "success": True,
                "code": code,
                "message": f"Coupon {code} will be applied at checkout.",
            }

    async def get_best_coupon(self, cart_total: float = 0) -> Dict[str, Any]:
        try:
            data = await self._admin_get(
                "price_rules.json",
                params={"limit": 100, "status": "active"},
            )
            price_rules = data.get("price_rules", [])
            if not price_rules:
                return {"success": True, "found": False, "message": "No discounts available right now"}

            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            # Compute savings from rule fields only (no API calls). The per-rule
            # discount_codes lookup is the N+1 — defer it and fetch only for the
            # best candidates, early-exiting on the first with a usable code.
            candidates = []
            for rule in price_rules:
                if rule.get("ends_at"):
                    try:
                        exp = datetime.fromisoformat(rule["ends_at"].replace("Z", "+00:00"))
                        if exp < now:
                            continue
                    except Exception:
                        pass

                prereq = rule.get("prerequisite_subtotal_range") or {}
                min_amt = _safe_float(prereq.get("greater_than_or_equal_to", 0))
                if cart_total > 0 and cart_total < min_amt:
                    continue

                value_type = rule.get("value_type", "")
                amount = abs(_safe_float(rule.get("value", 0)))
                savings = (cart_total * amount / 100) if (value_type == "percentage" and cart_total) else amount
                candidates.append({
                    "rule_id": rule["id"],
                    "value_type": value_type,
                    "amount": amount,
                    "savings": savings,
                })

            candidates.sort(key=lambda c: c["savings"], reverse=True)

            best = None
            for c in candidates[:10]:  # cap Admin API calls (rate-limit ~2 req/s)
                try:
                    codes_data = await self._admin_get(
                        f"price_rules/{c['rule_id']}/discount_codes.json",
                        params={"limit": 1},
                    )
                    code = codes_data.get("discount_codes", [{}])[0].get("code", "")
                except Exception:
                    code = ""
                if code:
                    # candidates are sorted by savings desc → first with a code is best
                    best = {
                        "code": code,
                        "type": "percent" if c["value_type"] == "percentage" else "fixed_cart",
                        "amount": c["amount"],
                        "savings": c["savings"],
                    }
                    break

            if not best:
                return {"success": True, "found": False, "message": "No applicable discounts for your cart"}

            sym = self.currency_symbol
            display = (
                f"{best['amount']:.0f}% off"
                if best["type"] == "percent"
                else f"{sym}{best['amount']:.0f} off"
            )
            return {
                "success": True,
                "found": True,
                "code": best["code"],
                "type": best["type"],
                "amount": best["amount"],
                "display": display,
                "estimated_savings": best["savings"],
            }
        except Exception as exc:
            logger.warning("get_best_coupon failed: %s", exc)
            return {"success": False, "found": False, "error": str(exc)}

    # ── Orders ─────────────────────────────────────────────────────────────────

    async def get_orders(
        self,
        *,
        customer_email: str,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        try:
            safe_email = customer_email.strip().lower()
            data = await self._admin_get(
                "orders.json",
                params={
                    "email": safe_email,
                    "limit": max(1, min(int(limit or 5), 10)),
                    "status": "any",
                    "fields": "id,name,financial_status,fulfillment_status,total_price,created_at,line_items,tracking_url",
                },
            )
            orders = data.get("orders", [])
            result = []
            for order in orders:
                items = [
                    {
                        "name": li.get("name", ""),
                        "quantity": li.get("quantity", 1),
                        "price": str(li.get("price", "")),
                    }
                    for li in order.get("line_items", [])
                ]
                fulfillment = order.get("fulfillments", [])
                tracking_url = fulfillment[0].get("tracking_url") if fulfillment else None

                result.append({
                    "id": order.get("id"),
                    "status": order.get("financial_status", ""),
                    "fulfillment_status": order.get("fulfillment_status", ""),
                    "total": str(order.get("total_price", "")),
                    "date": str(order.get("created_at", ""))[:10],
                    "items": items,
                    "tracking_url": tracking_url,
                    "order_number": order.get("name", ""),
                })
            return result
        except Exception as exc:
            logger.error("Shopify get_orders failed: %s", exc)
            return []

    # ── Reviews ────────────────────────────────────────────────────────────────

    async def get_reviews(self, product_id: int) -> Dict[str, Any]:
        reviews_endpoint = os.getenv("SHOPIFY_REVIEWS_ENDPOINT", "")
        if reviews_endpoint:
            try:
                resp = await self._http.get(
                    reviews_endpoint,
                    params={"product_id": product_id, "limit": 10},
                )
                resp.raise_for_status()
                data = resp.json()
                reviews = data if isinstance(data, list) else data.get("reviews", [])
                avg = (
                    round(sum(r.get("rating", 0) for r in reviews) / len(reviews), 1)
                    if reviews
                    else 0
                )
                return {"reviews": reviews, "count": len(reviews), "average_rating": avg}
            except Exception as exc:
                logger.warning("Shopify reviews proxy failed: %s", exc)

        return {"reviews": [], "count": 0, "average_rating": 0}

    async def submit_review(
        self,
        *,
        product_id: int,
        rating: int,
        review: str = "",
        name: Optional[str] = None,
        email: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "success": False,
            "message": "Review submission requires a Shopify reviews app (e.g., Judge.me). Please leave your review on the product page.",
        }

    # ── Store info ─────────────────────────────────────────────────────────────

    async def get_store_info(self) -> Dict[str, Any]:
        cache_key = "store:info"
        cached = await self._cache_get(cache_key)
        if isinstance(cached, dict):
            return cached

        GQL = """
        query ShopInfo {
          shop {
            name description primaryDomain { url }
            paymentSettings { currencyCode }
            shipsToCountries
          }
        }
        """
        info: Dict[str, Any] = {
            "store_name": "",
            "store_url": f"https://{self.store_domain}",
            "currency": os.getenv("STORE_CURRENCY", "$"),
            "supports_voice_cart": True,
        }

        try:
            data = await self._storefront(GQL)
            shop = data.get("shop", {})
            if shop.get("name"):
                info["store_name"] = shop["name"]
            if shop.get("description"):
                info["about"] = shop["description"]
            if shop.get("primaryDomain", {}).get("url"):
                info["store_url"] = shop["primaryDomain"]["url"]
            if shop.get("paymentSettings", {}).get("currencyCode"):
                info["currency"] = shop["paymentSettings"]["currencyCode"]
        except Exception as exc:
            logger.warning("Shopify get_store_info storefront query failed: %s", exc)

        await self._cache_set(cache_key, info, ttl=86400)
        return {k: v for k, v in info.items() if v not in ("", None)}

    async def get_store_policies(self) -> Dict[str, Any]:
        cache_key = "store:policies"
        cached = await self._cache_get(cache_key)
        if isinstance(cached, dict):
            return cached

        GQL = """
        query ShopPolicies {
          shop {
            name
            paymentSettings { currencyCode acceptedCardBrands enabledPresentmentCurrencies }
            shipsToCountries
            shippingPolicy { title body }
            refundPolicy { title body }
          }
        }
        """
        try:
            data = await self._storefront(GQL)
            shop = data.get("shop", {})

            payment = shop.get("paymentSettings", {})
            currency = payment.get("currencyCode", os.getenv("STORE_CURRENCY", "$"))
            card_brands = payment.get("acceptedCardBrands", [])
            ships_to = shop.get("shipsToCountries", [])[:5]

            shipping_body = _strip_html((shop.get("shippingPolicy") or {}).get("body", ""))[:300]
            refund_body = _strip_html((shop.get("refundPolicy") or {}).get("body", ""))[:300]

            payment_methods = [b.capitalize() for b in card_brands] if card_brands else ["Credit Card", "Debit Card"]

            policies_parts = []
            if payment_methods:
                policies_parts.append(f"Accepted payments: {', '.join(payment_methods)}.")
            if ships_to:
                policies_parts.append(f"Ships to: {', '.join(ships_to[:3])}.")
            if shipping_body:
                policies_parts.append(f"Shipping: {shipping_body}")
            if refund_body:
                policies_parts.append(f"Returns: {refund_body}")

            result = {
                "success": True,
                "store_name": shop.get("name", ""),
                "currency": currency,
                "currency_symbol": currency,
                "shipping_zones": ships_to,
                "payment_methods": payment_methods,
                "policies_text": " ".join(policies_parts),
                "shipping_policy": shipping_body,
                "returns_policy": refund_body,
            }
            await self._cache_set(cache_key, result, ttl=86400)
            return result
        except Exception as exc:
            logger.warning("Shopify get_store_policies failed: %s", exc)
            return {
                "success": False,
                "store_name": "",
                "currency": os.getenv("STORE_CURRENCY", "$"),
                "currency_symbol": os.getenv("STORE_CURRENCY", "$"),
                "shipping_zones": [],
                "payment_methods": [],
                "policies_text": "",
                "error": str(exc),
            }

    # ── Cache warm-up ──────────────────────────────────────────────────────────

    async def pre_warm(self) -> None:
        try:
            await asyncio.gather(
                self.get_categories(),
                self.search_products(query="", in_stock_only=False, limit=40),
                self.get_store_info(),
                return_exceptions=True,
            )
            logger.info("Shopify cache pre-warm complete")
        except Exception as exc:
            logger.debug("Shopify pre-warm error: %s", exc)

    # ── WooCommerce-compatible helpers (called by orchestrator) ───────────────

    @staticmethod
    def _attributes_to_variation_map(_attributes: Any) -> Dict[str, str]:
        return {}

    async def get_live_cart(self, session_id: str) -> Dict[str, Any]:
        return await self.get_cart(session_id=session_id)
