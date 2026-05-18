"""
Abstract base class that defines the exact interface every store client
(WooCommerce, Shopify, or any future platform) must implement.

The agent, orchestrator, tools, and routers only talk to this interface —
they never import WooCommerceClient or ShopifyClient directly.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class BaseStoreClient(ABC):
    """
    Platform-agnostic store interface.

    Every method here maps to one or more agent tools.
    Implementations must return data in the normalized shape
    the agent already understands (same keys, same types).
    """

    # ── Products ──────────────────────────────────────────────────────────────

    @abstractmethod
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
        """Search products by keyword, category, price range, or stock status."""

    @abstractmethod
    async def get_product_details(self, product_id: int) -> Dict[str, Any]:
        """Fetch full details for a single product by ID."""

    @abstractmethod
    async def get_product_variations(self, product_id: int) -> dict:
        """Get all variants (size/color combinations) for a variable product."""

    @abstractmethod
    async def find_variants(self, *, product_id: int) -> Dict[str, Any]:
        """Returns variant selector data for the chat widget UI."""

    @abstractmethod
    async def check_inventory(
        self,
        *,
        product_id: int,
        variation_id: Optional[int] = None,
        attributes: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Check stock for a specific product or variant."""

    @abstractmethod
    async def get_categories(self) -> List[Dict[str, Any]]:
        """Fetch all store categories / collections."""

    # ── Cart ──────────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_cart(self, *, session_id: str) -> Dict[str, Any]:
        """Fetch current cart for a session."""

    @abstractmethod
    async def get_cart_for_session(self, session_id: str) -> Dict[str, Any]:
        """Same as get_cart but accepts session_id as positional arg."""

    @abstractmethod
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
        """Add a product (with optional variant) to the cart."""

    @abstractmethod
    async def remove_from_cart(
        self,
        *,
        session_id: str,
        cart_item_key: Optional[str] = None,
        product_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Remove an item from the cart by key or product ID."""

    @abstractmethod
    async def update_cart_quantity(
        self,
        *,
        session_id: str,
        product_id: int,
        quantity: int,
    ) -> dict:
        """Update quantity of a cart item. If quantity <= 0, removes the item."""

    # ── Discounts ─────────────────────────────────────────────────────────────

    @abstractmethod
    async def apply_coupon(
        self,
        *,
        session_id: str,
        coupon_code: str,
    ) -> Dict[str, Any]:
        """Apply a discount/coupon code to the cart."""

    @abstractmethod
    async def get_best_coupon(self, cart_total: float = 0) -> dict:
        """Find the best available coupon for the current cart total."""

    # ── Orders ────────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_orders(
        self,
        *,
        customer_email: str,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Fetch recent orders for a customer by email."""

    # ── Reviews ───────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_reviews(self, product_id: int) -> dict:
        """Fetch reviews for a product."""

    @abstractmethod
    async def submit_review(
        self,
        *,
        product_id: int,
        rating: int,
        review: str = "",
        name: Optional[str] = None,
        email: Optional[str] = None,
    ) -> dict:
        """Submit a product review."""

    # ── Store info ────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_store_info(self) -> Dict[str, Any]:
        """Fetch general store information (name, currency, contact)."""

    @abstractmethod
    async def get_store_policies(self) -> dict:
        """Fetch store policies (shipping, returns, payment methods)."""

    # ── Cache warm-up ─────────────────────────────────────────────────────────

    @abstractmethod
    async def pre_warm(self) -> None:
        """Pre-fetch and cache frequently accessed data on startup."""

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @abstractmethod
    async def close(self) -> None:
        """Clean up resources (HTTP clients, connections)."""


# Alias for backwards compatibility with older imports
BaseCommerceClient = BaseStoreClient
