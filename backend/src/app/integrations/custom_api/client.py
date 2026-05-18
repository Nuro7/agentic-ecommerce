"""Generic REST store adapter stub — implement methods for custom store APIs."""
from typing import Any, Dict, List
from ..base.commerce import BaseStoreClient


class CustomApiClient(BaseStoreClient):
    """
    Plug-in adapter for any store that exposes a custom REST API.
    Set platform=custom_api in tenant config to use this adapter.
    All methods raise NotImplementedError until implemented.
    """

    def __init__(self, base_url: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    async def search_products(self, *, query: str, category_slug=None, min_price=None,
                               max_price=None, in_stock_only=True, on_sale=None, limit=6) -> List[Dict]:
        raise NotImplementedError

    async def get_product_details(self, product_id: int) -> Dict[str, Any]:
        raise NotImplementedError

    async def get_product_variations(self, product_id: int) -> dict:
        raise NotImplementedError

    async def find_variants(self, *, product_id: int) -> Dict[str, Any]:
        raise NotImplementedError

    async def check_inventory(self, *, product_id: int, variation_id=None, attributes=None) -> Dict[str, Any]:
        raise NotImplementedError

    async def get_categories(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    async def get_cart(self, *, session_id: str) -> Dict[str, Any]:
        raise NotImplementedError

    async def get_cart_for_session(self, session_id: str) -> Dict[str, Any]:
        return await self.get_cart(session_id=session_id)

    async def add_to_cart(self, *, session_id: str, product_id: int, variation_id=0,
                           quantity=1, variation=None, product_name=None, price=None) -> Dict[str, Any]:
        raise NotImplementedError

    async def remove_from_cart(self, *, session_id: str, cart_item_key=None, product_id=None) -> Dict[str, Any]:
        raise NotImplementedError

    async def update_cart_quantity(self, *, session_id: str, product_id: int, quantity: int) -> dict:
        raise NotImplementedError

    async def apply_coupon(self, *, session_id: str, coupon_code: str) -> Dict[str, Any]:
        raise NotImplementedError

    async def get_best_coupon(self, cart_total: float = 0) -> dict:
        raise NotImplementedError

    async def get_orders(self, *, customer_email: str, limit: int = 5) -> List[Dict[str, Any]]:
        raise NotImplementedError

    async def get_reviews(self, product_id: int) -> dict:
        raise NotImplementedError

    async def submit_review(self, *, product_id: int, rating: int, review: str = "",
                             name=None, email=None) -> dict:
        raise NotImplementedError

    async def get_store_info(self) -> Dict[str, Any]:
        raise NotImplementedError

    async def get_store_policies(self) -> dict:
        raise NotImplementedError

    async def pre_warm(self) -> None:
        pass

    async def close(self) -> None:
        pass
