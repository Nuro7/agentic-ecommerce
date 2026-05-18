"""
services/base_store_client.py

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
        """
        Search products by keyword, category, price range, or stock status.
        Returns a list of normalized product dicts.

        Normalized product shape:
        {
            "id": int,
            "name": str,
            "price": str,           # e.g. "24500"
            "regular_price": str,
            "sale_price": str,
            "on_sale": bool,
            "in_stock": bool,
            "stock_quantity": int | None,
            "description": str,
            "short_description": str,
            "images": [{"url": str}],
            "categories": [{"name": str, "slug": str}],
            "rating": float,
            "review_count": int,
            "url": str,
        }
        """

    @abstractmethod
    async def get_product_details(self, product_id: int) -> Dict[str, Any]:
        """
        Fetch full details for a single product by ID.
        Returns same normalized shape as search_products items,
        plus "variations" list.
        """

    @abstractmethod
    async def get_product_variations(self, product_id: int) -> dict:
        """
        Get all variants (size/color combinations) for a variable product.
        Returns:
        {
            "product_id": int,
            "variations": [
                {
                    "id": int,
                    "attributes": {"size": "M", "color": "Red"},
                    "price": str,
                    "in_stock": bool,
                    "stock_quantity": int | None,
                }
            ]
        }
        """

    @abstractmethod
    async def find_variants(self, *, product_id: int) -> Dict[str, Any]:
        """
        Returns variant selector data for the chat widget UI.
        Used to show size/color picker cards in the widget.
        """

    @abstractmethod
    async def check_inventory(
        self,
        *,
        product_id: int,
        variation_id: Optional[int] = None,
        attributes: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Check stock for a specific product or variant.
        Returns:
        {
            "product_id": int,
            "variation_id": int | None,
            "in_stock": bool,
            "stock_quantity": int | None,
            "status": str,   # "in_stock" | "out_of_stock" | "on_backorder"
        }
        """

    @abstractmethod
    async def get_categories(self) -> List[Dict[str, Any]]:
        """
        Fetch all store categories / collections.
        Returns:
        [{"id": int, "name": str, "slug": str, "count": int}]
        """

    # ── Cart ──────────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_cart(self, *, session_id: str) -> Dict[str, Any]:
        """
        Fetch current cart for a session.
        Returns:
        {
            "items": [
                {
                    "cart_item_key": str,
                    "product_id": int,
                    "variation_id": int,
                    "name": str,
                    "quantity": int,
                    "price": str,
                    "subtotal": str,
                }
            ],
            "item_count": int,
            "total": str,
            "is_empty": bool,
        }
        """

    @abstractmethod
    async def get_cart_for_session(self, session_id: str) -> Dict[str, Any]:
        """
        Same as get_cart but accepts session_id as positional arg.
        Used by greet endpoint.
        """

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
        """
        Add a product (with optional variant) to the cart.
        Returns updated cart dict (same shape as get_cart).
        For Shopify: also returns "checkout_url" key.
        """

    @abstractmethod
    async def remove_from_cart(
        self,
        *,
        session_id: str,
        cart_item_key: Optional[str] = None,
        product_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Remove an item from the cart by key or product ID.
        Returns updated cart dict.
        """

    @abstractmethod
    async def update_cart_quantity(
        self,
        *,
        session_id: str,
        product_id: int,
        quantity: int,
    ) -> dict:
        """
        Update quantity of a cart item.
        If quantity <= 0, removes the item.
        Returns updated cart dict.
        """

    # ── Discounts ─────────────────────────────────────────────────────────────

    @abstractmethod
    async def apply_coupon(
        self,
        *,
        session_id: str,
        coupon_code: str,
    ) -> Dict[str, Any]:
        """
        Apply a discount/coupon code to the cart.
        Returns:
        {
            "success": bool,
            "message": str,
            "discount_amount": str,
            "new_total": str,
        }
        """

    @abstractmethod
    async def get_best_coupon(self, cart_total: float = 0) -> dict:
        """
        Find the best available coupon for the current cart total.
        Returns:
        {
            "found": bool,
            "code": str,
            "type": str,        # "percent" | "fixed_cart"
            "amount": str,
            "description": str,
        }
        """

    # ── Orders ────────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_orders(
        self,
        *,
        customer_email: str,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Fetch recent orders for a customer by email.
        Returns:
        [
            {
                "id": int,
                "status": str,
                "total": str,
                "date": str,
                "items": [{"name": str, "quantity": int, "price": str}],
                "tracking_url": str | None,
            }
        ]
        """

    # ── Reviews ───────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_reviews(self, product_id: int) -> dict:
        """
        Fetch reviews for a product.
        Returns:
        {
            "product_id": int,
            "average_rating": float,
            "review_count": int,
            "reviews": [
                {
                    "author": str,
                    "rating": int,
                    "text": str,
                    "date": str,
                }
            ]
        }
        """

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
        """
        Submit a product review.
        Returns: {"success": bool, "message": str}
        """

    # ── Store info ────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_store_info(self) -> Dict[str, Any]:
        """
        Fetch general store information (name, currency, contact).
        Returns:
        {
            "name": str,
            "description": str,
            "url": str,
            "currency": str,
            "email": str,
        }
        """

    @abstractmethod
    async def get_store_policies(self) -> dict:
        """
        Fetch store policies (shipping, returns, payment methods).
        Returns:
        {
            "shipping": str,
            "returns": str,
            "payment_methods": [str],
        }
        """

    # ── Cache warm-up ─────────────────────────────────────────────────────────

    @abstractmethod
    async def pre_warm(self) -> None:
        """
        Pre-fetch and cache frequently accessed data on startup.
        Called as a background task when the backend starts.
        Implementations should cache: categories, top products, store info.
        """

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @abstractmethod
    async def close(self) -> None:
        """
        Clean up resources (HTTP clients, connections).
        Called on backend shutdown.
        """
