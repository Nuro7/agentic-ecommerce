"""Resolve the correct store client for a given platform and credentials."""
from .base.commerce import BaseCommerceClient


def create_store_client(platform: str, credentials: dict) -> BaseCommerceClient:
    if platform == "woocommerce":
        from .woocommerce.client import WooCommerceClient
        return WooCommerceClient(
            store_url=credentials["store_url"],
            consumer_key=credentials["consumer_key"],
            consumer_secret=credentials["consumer_secret"],
        )
    elif platform == "shopify":
        from .shopify.client import ShopifyClient
        return ShopifyClient(
            store_domain=credentials["store_domain"],
            storefront_token=credentials["storefront_token"],
            admin_token=credentials.get("admin_token", ""),
        )
    elif platform == "custom_api":
        from .custom_api.client import CustomApiClient
        return CustomApiClient(base_url=credentials["base_url"], api_key=credentials.get("api_key", ""))
    else:
        raise ValueError(f"Unknown platform: {platform}")
