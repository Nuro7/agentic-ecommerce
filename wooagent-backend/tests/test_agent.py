import json

import pytest

from agent.orchestrator import AgentOrchestrator
from agent.tools import execute_tool
from services.session import SessionService


class FakeRedis:
    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def delete(self, key):
        self.store.pop(key, None)


class FakeWooCommerceService:
    async def search_products(self, **kwargs):
        query = (kwargs.get("query") or "").lower()
        if "adidas" in query:
            return [
                {
                    "id": 202,
                    "name": "Adidas Ultraboost",
                    "price": "3499",
                    "sale_price": "",
                    "stock_status": "instock",
                    "image_url": "",
                    "permalink": "",
                    "short_description": "Running shoes",
                    "attributes": [],
                    "variations_summary": [],
                }
            ]
        return [
            {
                "id": 101,
                "name": "Nike Air Max",
                "price": "2999",
                "sale_price": "",
                "stock_status": "instock",
                "image_url": "",
                "permalink": "",
                "short_description": "Running shoes",
                "attributes": [],
                "variations_summary": [],
            }
        ]

    async def get_product_details(self, product_id):
        return {
            "id": product_id,
            "name": "Nike Air Max" if int(product_id) == 101 else "Adidas Ultraboost",
            "price": "2999",
            "stock_status": "instock",
            "stock_quantity": 2,
            "variations": [],
        }

    async def check_inventory(self, **kwargs):
        return {
            "product_id": kwargs.get("product_id"),
            "variation_id": kwargs.get("variation_id") or 0,
            "in_stock": True,
            "stock_quantity": 2,
            "attributes": [],
        }

    async def get_cart(self, *, session_id):
        return {"count": 1, "total": "2999", "items": [{"cart_item_key": "abc", "name": "Nike Air Max", "product_id": 101}]}

    async def add_to_cart(self, **kwargs):
        return {
            "success": True,
            "cart_count": int(kwargs.get("quantity", 1)),
            "cart_total": "2999",
            "message": "Item added",
        }

    async def remove_from_cart(self, **kwargs):
        return {"success": True}

    async def get_orders(self, **kwargs):
        return []

    async def apply_coupon(self, **kwargs):
        return {"success": True, "code": kwargs.get("coupon_code", "")}

    async def get_categories(self):
        return [{"id": 1, "name": "Shoes"}]


class FakeTTSService:
    async def synthesize(self, text):
        return None


@pytest.mark.asyncio
async def test_product_search_flow():
    wc = FakeWooCommerceService()

    execution = await execute_tool(
        "search_products",
        {"query": "running shoes", "max_price": 3000, "limit": 5},
        session_id="session-1",
        woocommerce_service=wc,
    )

    assert "products" in execution.result
    assert execution.result["products"][0]["name"] == "Nike Air Max"
    assert execution.action["type"] == "show_products"


@pytest.mark.asyncio
async def test_add_to_cart_flow():
    wc = FakeWooCommerceService()

    execution = await execute_tool(
        "add_to_cart",
        {"product_id": 101, "variation_id": 0, "quantity": 1},
        session_id="session-1",
        woocommerce_service=wc,
    )

    assert execution.result["cart"]["success"] is True
    assert execution.action["type"] == "add_to_cart"
    assert execution.action["payload"]["product_id"] == 101


@pytest.mark.asyncio
async def test_session_persistence():
    redis_client = FakeRedis()
    service = SessionService(redis_client)

    await service.update_session(
        session_id="persist-1",
        conversation_history=[{"role": "user", "content": "hello"}],
        cart_snapshot={"count": 1},
        customer_email="john@example.com",
    )

    state = await service.get_session("persist-1")

    assert state["conversation_history"][0]["content"] == "hello"
    assert state["cart_snapshot"]["count"] == 1
    assert state["customer_email"] == "john@example.com"

    raw = await redis_client.get("session:persist-1")
    assert raw is not None
    parsed = json.loads(raw)
    assert parsed["customer_email"] == "john@example.com"


@pytest.mark.asyncio
async def test_fast_intent_compare_flow():
    """compare/availability queries go through LLM; _run_fast_intent falls
    back to a generic product search and returns show_products as a safety net."""
    wc = FakeWooCommerceService()
    orchestrator = AgentOrchestrator(wc, SessionService(FakeRedis()), FakeTTSService())

    result = await orchestrator._run_fast_intent(
        "compare nike air max vs adidas ultraboost",
        "session-1",
    )

    # Fast-intent doesn't handle compare directly — it runs a product-search
    # fallback so the user always gets something useful even without an LLM.
    assert result is not None
    action_types = [item.get("type") for item in result["actions"]]
    assert "show_products" in action_types


@pytest.mark.asyncio
async def test_fast_intent_availability_flow():
    """Availability queries go to the LLM; fast_intent falls back to a
    product search so the widget always has something to display."""
    wc = FakeWooCommerceService()
    orchestrator = AgentOrchestrator(wc, SessionService(FakeRedis()), FakeTTSService())

    result = await orchestrator._run_fast_intent(
        "is nike air max size 9 available",
        "session-1",
    )

    assert result is not None
    action_types = [item.get("type") for item in result["actions"]]
    assert "show_products" in action_types
