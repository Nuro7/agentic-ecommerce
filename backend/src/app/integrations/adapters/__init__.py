"""Adapter layer — converts raw platform dicts to CanonicalProduct.

Usage:
    from .adapters import CanonicalProduct, ShopifyAdapter, WooAdapter, CustomAdapter

    # Shopify
    product = ShopifyAdapter.normalize(raw_node, tenant_id="t_123")

    # WooCommerce
    product = WooAdapter.normalize(raw_dict, tenant_id="t_456")

    # Custom storefront with JSONPath mapping
    product = CustomAdapter.normalize(raw, mapping={"name": "title", "price": "pricing.amount"})
"""
from .canonical import CanonicalProduct, CanonicalVariant
from .shopify_adapter import ShopifyAdapter
from .woo_adapter import WooAdapter
from .custom_adapter import CustomAdapter

__all__ = [
    "CanonicalProduct",
    "CanonicalVariant",
    "ShopifyAdapter",
    "WooAdapter",
    "CustomAdapter",
]
