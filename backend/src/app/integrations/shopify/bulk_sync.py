"""Shopify Bulk Operations API — unlimited product export for large stores.

Regular paginated GraphQL caps at ~40 products per request and hits the
API cost limit at ~1 000 products.  Bulk Operations bypass both limits:

  1. POST bulkOperationRunQuery mutation  → returns an operation ID
  2. Poll currentBulkOperation            → wait for COMPLETED status
  3. GET the signed JSONL download URL    → stream & parse all products
  4. Reconstruct nodes into the same dict shape _normalize_product_node()
     already understands, so the existing ShopifyAdapter pipeline is reused.

Typical timings:
  - 500 products  →  ~15–30 s
  - 5 000 products →  ~60–120 s
  - 50 000 products → ~5–10 min

The task timeout is set to 600 s (10 min), giving headroom for very large stores.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


# ── GraphQL documents ─────────────────────────────────────────────────────────

# Admin GraphQL query embedded inside the bulk mutation. NOTE: this runs against
# the ADMIN API, so it must use Admin field names — NOT the Storefront ones:
#   • Product has no `availableForSale` / `priceRange` → use `status`/`totalInventory`
#     and `priceRangeV2`.
#   • `compareAtPriceRange` exposes `minVariantCompareAtPrice` (not `minVariantPrice`).
#   • ProductVariant `price` is a Money SCALAR (no `{ amount }`) and stock is
#     `inventoryQuantity` (not `quantityAvailable`).
# _parse_bulk_jsonl() remaps these back to the Storefront-shaped dict that
# ShopifyClient._normalize_product_node() consumes, so the adapter pipeline is unchanged.
# - featuredImage / priceRangeV2 / compareAtPriceRange / options → inlined objects.
# - variants is a Connection → each variant gets its own JSONL line with __parentId.
_BULK_INNER_QUERY = """
{
  products {
    edges {
      node {
        id
        title
        handle
        description
        status
        totalInventory
        tags
        priceRangeV2 {
          minVariantPrice { amount currencyCode }
          maxVariantPrice { amount currencyCode }
        }
        compareAtPriceRange {
          minVariantCompareAtPrice { amount currencyCode }
        }
        featuredImage { url }
        options { name values }
        collections(first: 5) {
          edges {
            node {
              id
              title
              handle
            }
          }
        }
        variants {
          edges {
            node {
              id
              availableForSale
              inventoryQuantity
              price
              compareAtPrice
              selectedOptions { name value }
            }
          }
        }
      }
    }
  }
}
"""

_START_MUTATION = """
mutation BulkProductExport($query: String!) {
  bulkOperationRunQuery(query: $query) {
    bulkOperation {
      id
      status
    }
    userErrors {
      field
      message
    }
  }
}
"""

_POLL_QUERY = """
query CurrentBulkOp {
  currentBulkOperation {
    id
    status
    errorCode
    objectCount
    fileSize
    url
    partialDataUrl
  }
}
"""


# ═══════════════════════════════════════════════════════════════════════════════
# JSONL parser
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_bulk_jsonl(text: str) -> List[Dict[str, Any]]:
    """Convert Shopify bulk JSONL text into a list of product-node dicts.

    Shopify serialises each GraphQL node on its own line.  Connection children
    (variants) carry a ``__parentId`` field pointing to their parent product.
    Non-connection fields (priceRange, featuredImage, options) are inlined in
    the product line.

    Returns nodes in the same shape ShopifyClient._normalize_product_node()
    expects, so the existing adapter pipeline needs no changes.
    """
    products: Dict[str, Dict[str, Any]] = {}   # gid → product dict
    variants_map: Dict[str, List[Dict]] = {}   # product_gid → [variant, ...]
    collections_map: Dict[str, List[Dict]] = {}  # product_gid → [collection, ...]

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj: Dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("Skipping malformed JSONL line: %.80s", line)
            continue

        gid: str = obj.get("id", "")
        parent_id: Optional[str] = obj.get("__parentId")

        if parent_id:
            if "/ProductVariant/" in gid:
                variants_map.setdefault(parent_id, []).append(obj)
            elif "/Collection/" in gid:
                collections_map.setdefault(parent_id, []).append(obj)
        elif "/Product/" in gid and "/ProductVariant/" not in gid:
            products[gid] = obj

    # Reconstruct nodes in the shape _normalize_product_node() consumes
    nodes: List[Dict[str, Any]] = []
    for gid, p in products.items():
        product_variants = variants_map.get(gid, [])

        variant_edges = [
            {
                "node": {
                    "id":               v.get("id", ""),
                    "availableForSale": v.get("availableForSale", False),
                    # Admin `inventoryQuantity` (Int) → Storefront `quantityAvailable`.
                    "quantityAvailable": v.get("inventoryQuantity"),
                    # Admin `price` is a Money SCALAR string → wrap as {amount} so the
                    # normalizer's v["price"]["amount"] lookup keeps working.
                    "price":            {"amount": str(v.get("price") or "0")},
                    "selectedOptions":  v.get("selectedOptions") or [],
                }
            }
            for v in product_variants[:10]
        ]

        # featuredImage is inlined as {"url": "..."} (not a connection)
        featured = p.get("featuredImage") or {}
        image_url = featured.get("url", "")
        image_edges = [{"node": {"url": image_url}}] if image_url else []

        # Admin API → Storefront-shaped remap for the normalizer:
        #  • Admin Product has no `availableForSale` → derive from any purchasable
        #    variant, else an ACTIVE status.
        #  • `priceRangeV2` has the same MoneyV2 shape as Storefront `priceRange`.
        #  • `compareAtPriceRange.minVariantCompareAtPrice` → `minVariantPrice`.
        prod_available = any(v.get("availableForSale") for v in product_variants) or (
            str(p.get("status") or "").upper() == "ACTIVE"
        )
        price_range = p.get("priceRangeV2") or {}
        cap_raw = p.get("compareAtPriceRange") or {}
        compare_at_range: Dict[str, Any] = {}
        if cap_raw.get("minVariantCompareAtPrice"):
            compare_at_range = {"minVariantPrice": cap_raw["minVariantCompareAtPrice"]}

        raw_tags = p.get("tags")
        parsed_tags: list[str] = []
        if isinstance(raw_tags, list):
            parsed_tags = [str(t) for t in raw_tags if t]
        elif isinstance(raw_tags, str):
            parsed_tags = [t.strip() for t in raw_tags.split(",") if t.strip()]

        product_collections = collections_map.get(gid, [])
        collection_edges = [
            {
                "node": {
                    "id":     c.get("id", ""),
                    "title":  c.get("title", ""),
                    "handle": c.get("handle", ""),
                }
            }
            for c in product_collections[:5]
        ]

        node: Dict[str, Any] = {
            "id":                  gid,
            "title":               p.get("title", ""),
            "handle":              p.get("handle", ""),
            "description":         p.get("description", ""),
            "availableForSale":    prod_available,
            "totalInventory":      p.get("totalInventory"),
            "tags":                parsed_tags,
            "priceRange":          price_range,
            "compareAtPriceRange": compare_at_range,
            "options":             p.get("options") or [],
            "images":              {"edges": image_edges},
            "collections":         {"edges": collection_edges},
            "variants":            {"edges": variant_edges},
            "featuredImage":       featured,
        }
        nodes.append(node)

    logger.info("Parsed %d products from bulk JSONL (%d variant lines)", len(nodes), sum(len(v) for v in variants_map.values()))
    return nodes


# ═══════════════════════════════════════════════════════════════════════════════
# Bulk sync class
# ═══════════════════════════════════════════════════════════════════════════════

class ShopifyBulkSync:
    """Manages the full lifecycle of a Shopify Bulk Operations product export.

    Usage::

        async with ShopifyBulkSync(admin_token, store_domain, api_version) as bulk:
            nodes = await bulk.fetch_all_products(timeout=600)
        # nodes is a list of dicts ready for ShopifyClient._normalize_product_node()
    """

    def __init__(
        self,
        admin_token: str,
        store_domain: str,
        api_version: str,
    ) -> None:
        self.admin_token = admin_token
        self._graphql_url = (
            f"https://{store_domain}/admin/api/{api_version}/graphql.json"
        )
        # Longer timeout: bulk JSONL files can be tens of MB
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            follow_redirects=True,
        )

    async def __aenter__(self) -> "ShopifyBulkSync":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self._http.aclose()

    async def close(self) -> None:
        await self._http.aclose()

    # ── Internal helpers ───────────────────────────────────────────────────────

    async def _admin_graphql(self, query: str, variables: Optional[Dict] = None) -> Dict[str, Any]:
        resp = await self._http.post(
            self._graphql_url,
            json={"query": query, "variables": variables or {}},
            headers={
                "X-Shopify-Access-Token": self.admin_token,
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        body: Dict[str, Any] = resp.json()
        if body.get("errors"):
            raise RuntimeError(f"Shopify Admin GraphQL error: {body['errors']}")
        return body.get("data", {})

    # ── Lifecycle steps ────────────────────────────────────────────────────────

    async def _start_bulk_operation(self) -> str:
        """Start a new bulk product export. Returns the operation GID."""
        data = await self._admin_graphql(
            _START_MUTATION,
            {"query": _BULK_INNER_QUERY},
        )
        op_result = data.get("bulkOperationRunQuery", {})
        user_errors = op_result.get("userErrors") or []
        if user_errors:
            messages = "; ".join(e.get("message", "") for e in user_errors)
            raise RuntimeError(f"Bulk operation start failed: {messages}")
        bulk_op = op_result.get("bulkOperation") or {}
        op_id: Optional[str] = bulk_op.get("id")
        if not op_id:
            raise RuntimeError("Shopify returned no operation ID for bulk mutation")
        logger.info("Bulk operation started: id=%s status=%s", op_id, bulk_op.get("status"))
        return op_id

    async def _poll_until_complete(
        self,
        expected_op_id: str,
        timeout: int = 600,
    ) -> str:
        """Poll currentBulkOperation until it reaches COMPLETED.

        Returns the JSONL download URL.
        Raises TimeoutError or RuntimeError on failure / cancellation.
        """
        import time
        deadline = time.monotonic() + timeout
        wait = 5.0  # initial poll interval

        while time.monotonic() < deadline:
            await asyncio.sleep(wait)
            wait = min(wait * 1.5, 30.0)  # exponential back-off, cap at 30 s

            data = await self._admin_graphql(_POLL_QUERY)
            op = data.get("currentBulkOperation") or {}
            status = op.get("status", "")
            op_id = op.get("id", "")

            # If a different operation is now current, ours may have been
            # superseded — treat it as failed so we fall back to paginated.
            if op_id and op_id != expected_op_id and status not in ("CREATED", "RUNNING"):
                raise RuntimeError(
                    f"Bulk operation superseded (current={op_id}, expected={expected_op_id})"
                )

            logger.info(
                "Bulk op poll: status=%s objects=%s size=%s",
                status,
                op.get("objectCount", "?"),
                op.get("fileSize", "?"),
            )

            if status == "COMPLETED":
                url = op.get("url")
                if not url:
                    raise RuntimeError("Bulk operation COMPLETED but URL is empty")
                return url

            if status in ("FAILED", "CANCELED"):
                raise RuntimeError(
                    f"Bulk operation ended with status={status} "
                    f"errorCode={op.get('errorCode')}"
                )

            # CREATED / RUNNING — keep waiting

        raise TimeoutError(
            f"Shopify bulk operation did not complete within {timeout} seconds"
        )

    async def _download_jsonl(self, url: str) -> str:
        """Download the signed JSONL file from Shopify's CDN. Returns raw text."""
        logger.info("Downloading bulk JSONL from Shopify CDN…")
        resp = await self._http.get(url)
        resp.raise_for_status()
        logger.info(
            "Bulk JSONL downloaded: %d bytes", len(resp.content)
        )
        return resp.text

    # ── Public API ─────────────────────────────────────────────────────────────

    async def fetch_all_products(self, timeout: int = 600) -> List[Dict[str, Any]]:
        """Full bulk lifecycle: start → poll → download → parse.

        Returns a list of product-node dicts compatible with
        ShopifyClient._normalize_product_node().  May return an empty list if
        the store has no products.

        Raises RuntimeError / TimeoutError on failure — caller should fall back
        to paginated sync.
        """
        op_id = await self._start_bulk_operation()
        download_url = await self._poll_until_complete(op_id, timeout=timeout)
        jsonl_text = await self._download_jsonl(download_url)
        return _parse_bulk_jsonl(jsonl_text)
