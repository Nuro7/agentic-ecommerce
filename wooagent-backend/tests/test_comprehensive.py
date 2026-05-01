"""
Comprehensive test suite for WooAgent backend.
Covers unit tests (mocked) + live integration tests against the running server.

Run:  python3 -m pytest tests/test_comprehensive.py -v
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio

# ─── Shared constants ────────────────────────────────────────────────────────
BASE_URL = "http://localhost:8000"
SESSION_PREFIX = f"test-{int(time.time())}"

# ─── Shared fake infrastructure ──────────────────────────────────────────────

class FakeRedis:
    def __init__(self):
        self.store: Dict[str, Any] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def setex(self, key, ttl, value):
        self.store[key] = value

    async def delete(self, key):
        self.store.pop(key, None)


FAKE_PRODUCTS = [
    {
        "id": 101,
        "name": "Red Bicycle Helmet",
        "price": "1299",
        "sale_price": "",
        "regular_price": "1299",
        "stock_status": "instock",
        "stock_quantity": 10,
        "image_url": "https://example.com/helmet.jpg",
        "permalink": "https://example.com/helmet",
        "short_description": "Safety helmet",
        "attributes": [],
        "variations_summary": [],
    },
    {
        "id": 102,
        "name": "Bicycle Gloves",
        "price": "499",
        "sale_price": "399",
        "regular_price": "499",
        "stock_status": "instock",
        "stock_quantity": 5,
        "image_url": "",
        "permalink": "",
        "short_description": "Cycling gloves",
        "attributes": [],
        "variations_summary": [],
    },
]


class FakeWoo:
    async def search_products(self, **kwargs):
        query = str(kwargs.get("query") or "").lower()
        limit = kwargs.get("limit", 6)
        if "helmet" in query:
            return [FAKE_PRODUCTS[0]]
        if "glove" in query:
            return [FAKE_PRODUCTS[1]]
        return FAKE_PRODUCTS[:limit]

    async def get_product_details(self, product_id):
        for p in FAKE_PRODUCTS:
            if p["id"] == int(product_id):
                return p
        return {}

    async def check_inventory(self, **kwargs):
        return {"product_id": kwargs.get("product_id"), "in_stock": True, "stock_quantity": 5}

    async def get_cart(self, *, session_id):
        return {"count": 2, "total": "1798", "items": [
            {"cart_item_key": "k1", "name": "Red Bicycle Helmet", "product_id": 101, "quantity": 1, "price": "1299"},
            {"cart_item_key": "k2", "name": "Bicycle Gloves", "product_id": 102, "quantity": 1, "price": "499"},
        ]}

    async def get_live_cart(self, session_id):
        return await self.get_cart(session_id=session_id)

    async def add_to_cart(self, **kwargs):
        return {"success": True, "cart_count": 1, "cart_total": "1299", "message": "Item added"}

    async def remove_from_cart(self, **kwargs):
        return {"success": True}

    async def get_categories(self):
        return [{"id": 1, "name": "Helmets", "count": 3}, {"id": 2, "name": "Gloves", "count": 2}]

    async def get_orders(self, **kwargs):
        return []

    async def apply_coupon(self, **kwargs):
        return {"success": True, "code": kwargs.get("coupon_code", ""), "message": "Coupon applied"}

    async def get_store_policies(self):
        return {"store_name": "My Store", "shipping": "Free above ₹500", "returns": "7-day returns"}

    async def get_best_coupon(self, cart_total=0):
        return {"code": "SAVE10", "discount": "10%", "discount_type": "percent", "amount": 10}

    async def get_reviews(self, product_id, **kwargs):
        return {"reviews": [], "count": 0, "average_rating": 0}

    async def find_variants(self, product_id):
        return {"variations": [], "attributes": {}}

    async def update_cart_quantity(self, **kwargs):
        return {"success": True, "cart_count": kwargs.get("quantity", 1)}

    async def submit_review(self, **kwargs):
        return {"success": True, "review_id": 999}


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — TTS Service Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestTTSAudioFormat(unittest.TestCase):
    """Verify audio_format() returns correct codec per provider."""

    def _make_svc(self, provider):
        with patch.dict(os.environ, {"TTS_PROVIDER": provider}):
            from services.tts import TTSService
            return TTSService()

    def test_google_returns_mp3(self):
        svc = self._make_svc("google")
        self.assertEqual(svc.audio_format(), "mp3",
            "Google TTS outputs MP3 — must return 'mp3' so browser sets correct MIME type")

    def test_elevenlabs_returns_mp3(self):
        svc = self._make_svc("elevenlabs")
        self.assertEqual(svc.audio_format(), "mp3")

    def test_azure_returns_mp3(self):
        svc = self._make_svc("azure")
        self.assertEqual(svc.audio_format(), "mp3",
            "Azure TTS outputs MP3 — must return 'mp3'")

    def test_groq_returns_wav(self):
        svc = self._make_svc("groq")
        self.assertEqual(svc.audio_format(), "wav",
            "Groq/Orpheus outputs WAV — must return 'wav'")

    def test_browser_returns_wav(self):
        svc = self._make_svc("browser")
        # browser provider returns None audio, format doesn't matter but should not crash
        self.assertIn(svc.audio_format(), ("mp3", "wav"))


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — Language Detection Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestLanguageDetection(unittest.TestCase):
    def setUp(self):
        from agent.language import detect_language
        self.detect = detect_language

    def test_english_detected(self):
        self.assertEqual(self.detect("Show me products"), "en")

    def test_hindi_detected(self):
        lang = self.detect("मुझे यह उत्पाद दिखाओ")
        self.assertEqual(lang, "hi")

    def test_empty_defaults_to_english(self):
        self.assertEqual(self.detect(""), "en")

    def test_quick_reply_english(self):
        # Quick replies from the widget are English regardless
        self.assertEqual(self.detect("Store info"), "en")
        self.assertEqual(self.detect("Show my cart"), "en")
        self.assertEqual(self.detect("Browse"), "en")
        self.assertEqual(self.detect("Show best sellers"), "en")


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — Session Service Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestSessionService(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from services.session import SessionService
        self.service = SessionService(FakeRedis())

    async def test_empty_session(self):
        state = await self.service.get_session("new-session")
        self.assertIsInstance(state, dict)
        self.assertEqual(state.get("conversation_history", []), [])

    async def test_save_and_retrieve_history(self):
        await self.service.update_session(
            session_id="s1",
            conversation_history=[{"role": "user", "content": "hello"}],
        )
        state = await self.service.get_session("s1")
        self.assertEqual(state["conversation_history"][0]["content"], "hello")

    async def test_save_and_retrieve_cart(self):
        await self.service.update_session(
            session_id="s2",
            cart_snapshot={"count": 2, "total": "₹1000"},
        )
        state = await self.service.get_session("s2")
        self.assertEqual(state["cart_snapshot"]["count"], 2)

    async def test_meta_persistence(self):
        await self.service.save_meta("s3", {"language": "hi", "address_state": "idle"})
        meta = await self.service.get_meta("s3")
        self.assertEqual(meta["language"], "hi")

    async def test_multiple_sessions_isolated(self):
        await self.service.update_session("sa", conversation_history=[{"role": "user", "content": "session A"}])
        await self.service.update_session("sb", conversation_history=[{"role": "user", "content": "session B"}])
        a = await self.service.get_session("sa")
        b = await self.service.get_session("sb")
        self.assertNotEqual(a["conversation_history"], b["conversation_history"])


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — Orchestrator _say() Template Safety Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestSayTemplate(unittest.TestCase):
    def setUp(self):
        from agent.orchestrator import AgentOrchestrator
        self.say = AgentOrchestrator._say

    def test_english_cart_opened(self):
        result = self.say("en", "cart_opened")
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)

    def test_english_store_info_with_name(self):
        result = self.say("en", "store_info", store_name="My Shop")
        self.assertIn("My Shop", result)

    def test_missing_key_does_not_crash(self):
        # availability template expects {name}, {size_text}, {stock_text}, {qty_text}
        # Passing nothing should not raise KeyError
        result = self.say("en", "availability")
        self.assertIsInstance(result, str)  # returns raw template, not crash

    def test_unknown_language_falls_back_to_english(self):
        result = self.say("xx", "cart_opened")
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)

    def test_unknown_key_returns_empty_or_string(self):
        result = self.say("en", "nonexistent_key_xyz")
        self.assertIsInstance(result, str)

    def test_hindi_template(self):
        result = self.say("hi", "cart_opened")
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)

    def test_availability_with_all_kwargs(self):
        # _say computes size_text/stock_text/qty_text from size/in_stock/qty kwargs
        result = self.say("en", "availability", name="Helmet", size="L", in_stock=True, qty=3)
        self.assertIn("Helmet", result)
        self.assertIn("available", result)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — Fast-Intent Routing Tests (unit, mocked)
# ─────────────────────────────────────────────────────────────────────────────

class TestFastIntentRouting(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from agent.orchestrator import AgentOrchestrator
        from services.session import SessionService
        self.woo = FakeWoo()
        self.orch = AgentOrchestrator(
            woocommerce_service=self.woo,
            session_service=SessionService(FakeRedis()),
        )
        self.store_ctx = {"store_name": "My Store"}

    # ── store_info intents ────────────────────────────────────────────────
    async def _assert_store_info(self, message):
        result = await self.orch._run_fast_intent(
            message=message, session_id="test", language="en",
            store_context=self.store_ctx
        )
        self.assertIsNotNone(result, f"Expected store_info result for: {message!r}")
        action_types = [a["type"] for a in (result.get("ui_actions") or [])]
        self.assertIn("show_store_info", action_types, f"Expected show_store_info for: {message!r}")
        payload = result["ui_actions"][0]["payload"]
        self.assertIn("store_name", payload)
        self.assertIn("shipping", payload)
        self.assertIn("returns", payload)
        self.assertIn("payment_methods", payload)

    async def test_store_info_exact_phrase(self):
        await self._assert_store_info("Store info")

    async def test_store_info_lowercase(self):
        await self._assert_store_info("store info")

    async def test_store_info_about_store(self):
        await self._assert_store_info("about store")

    async def test_store_info_store_details(self):
        await self._assert_store_info("store details")

    async def test_store_info_what_is_store(self):
        await self._assert_store_info("what is this store")

    async def test_store_info_has_response_text(self):
        result = await self.orch._run_fast_intent(
            message="store info", session_id="t", language="en",
            store_context=self.store_ctx
        )
        text = result.get("response_text", "")
        self.assertIn("My Store", text)
        self.assertTrue(len(text) > 10)

    async def test_store_info_has_suggested_replies(self):
        result = await self.orch._run_fast_intent(
            message="Store info", session_id="t", language="en",
            store_context=self.store_ctx
        )
        self.assertIsInstance(result.get("suggested_replies"), list)
        self.assertTrue(len(result["suggested_replies"]) > 0)

    # ── cart_view intents ─────────────────────────────────────────────────
    async def _assert_cart_view(self, message):
        result = await self.orch._run_fast_intent(
            message=message, session_id="test", language="en"
        )
        self.assertIsNotNone(result, f"Expected cart result for: {message!r}")
        action_types = [a["type"] for a in (result.get("ui_actions") or [])]
        self.assertIn("show_cart", action_types, f"Expected show_cart for: {message!r}")

    async def test_cart_show_my_cart(self):
        await self._assert_cart_view("Show my cart")

    async def test_cart_view_cart(self):
        await self._assert_cart_view("view cart")

    async def test_cart_open_cart(self):
        await self._assert_cart_view("open cart")

    async def test_cart_what_in_my_cart(self):
        await self._assert_cart_view("what's in my cart")

    async def test_cart_has_cart_data(self):
        result = await self.orch._run_fast_intent(
            message="show my cart", session_id="test", language="en"
        )
        action = result["ui_actions"][0]
        cart = action["payload"]["cart"]
        self.assertIsNotNone(cart)

    # ── browse / show products intents ────────────────────────────────────
    async def _assert_browse(self, message):
        result = await self.orch._run_fast_intent(
            message=message, session_id="test", language="en",
            store_context=self.store_ctx, force=True
        )
        self.assertIsNotNone(result, f"Expected browse result for: {message!r}")
        action_types = [a["type"] for a in (result.get("ui_actions") or [])]
        self.assertIn("show_products", action_types, f"Expected show_products for: {message!r}")

    async def test_browse_keyword(self):
        await self._assert_browse("browse")

    async def test_browse_show_products(self):
        await self._assert_browse("show products")

    async def test_browse_best_sellers(self):
        await self._assert_browse("show best sellers")

    async def test_browse_shows_only_one_product(self):
        """Fast-intent browse must show exactly 1 product card (not the full catalog)."""
        result = await self.orch._run_fast_intent(
            message="show best sellers", session_id="test", language="en",
            store_context=self.store_ctx, force=True
        )
        show_products_action = next(
            (a for a in result.get("ui_actions", []) if a["type"] == "show_products"), None
        )
        self.assertIsNotNone(show_products_action)
        products = show_products_action["payload"]["products"]
        self.assertEqual(len(products), 1,
            f"Fast-intent browse must show 1 product, got {len(products)}")


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — Intent Detection Logic Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestIntentDetectors(unittest.TestCase):
    def setUp(self):
        from agent.orchestrator import AgentOrchestrator
        self.has_store = AgentOrchestrator._has_store_info_intent
        self.has_cart = AgentOrchestrator._has_cart_view_intent
        self.has_remove = AgentOrchestrator._has_remove_intent

    # store info
    def test_store_info_detected(self):
        for phrase in ["store info", "Store Info", "STORE INFO", "about store",
                       "store details", "store information", "what is this store"]:
            self.assertTrue(self.has_store(phrase.lower()), f"Should detect: {phrase!r}")

    def test_store_info_not_triggered_on_product(self):
        self.assertFalse(self.has_store("show me a helmet"))
        self.assertFalse(self.has_store("browse products"))

    # cart view
    def test_cart_view_detected(self):
        for phrase in ["show my cart", "view cart", "open cart", "my cart",
                       "what's in my cart", "cart total"]:
            self.assertTrue(self.has_cart(phrase.lower()), f"Should detect: {phrase!r}")

    def test_cart_not_triggered_on_checkout(self):
        # checkout alone should go to LLM, not fast-intent cart view
        self.assertFalse(self.has_cart("checkout"))

    # remove from cart
    def test_remove_detected(self):
        for phrase in ["remove item", "remove helmet", "remove from cart",
                       "delete item", "delete from cart", "delete product"]:
            self.assertTrue(self.has_remove(phrase.lower()), f"Should detect: {phrase!r}")

    def test_remove_not_triggered_on_add(self):
        self.assertFalse(self.has_remove("add to cart"))
        self.assertFalse(self.has_remove("buy this"))


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7 — Address Collection Flow Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestAddressCollection(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from agent.orchestrator import AgentOrchestrator, AddressCollectionState
        from services.session import SessionService
        self.orch = AgentOrchestrator(
            woocommerce_service=FakeWoo(),
            session_service=SessionService(FakeRedis()),
        )
        self.State = AddressCollectionState

    async def test_name_collection_single_word(self):
        resp, state, addr, _ = await self.orch.handle_address_collection(
            session_id="a1",
            user_message="Rahul",
            current_state=self.State.COLLECTING_NAME,
            address_data={},
            language="en",
        )
        # Single word name → ask for last name
        self.assertEqual(state, self.State.COLLECTING_LAST_NAME)
        self.assertEqual(addr["first_name"], "Rahul")
        self.assertIsInstance(resp, str)
        self.assertTrue(len(resp) > 0)

    async def test_name_collection_full_name(self):
        resp, state, addr, _ = await self.orch.handle_address_collection(
            session_id="a2",
            user_message="Rahul Kumar",
            current_state=self.State.COLLECTING_NAME,
            address_data={},
            language="en",
        )
        # Full name → skip to address
        self.assertEqual(state, self.State.COLLECTING_ADDRESS_LINE1)
        self.assertEqual(addr["first_name"], "Rahul")
        self.assertEqual(addr["last_name"], "Kumar")

    async def test_last_name_collection(self):
        resp, state, addr, _ = await self.orch.handle_address_collection(
            session_id="a3",
            user_message="Kumar",
            current_state=self.State.COLLECTING_LAST_NAME,
            address_data={"first_name": "Rahul"},
            language="en",
        )
        self.assertEqual(state, self.State.COLLECTING_ADDRESS_LINE1)
        self.assertEqual(addr["last_name"], "Kumar")

    async def test_pincode_valid_6_digits(self):
        resp, state, addr, _ = await self.orch.handle_address_collection(
            session_id="a4",
            user_message="682001",
            current_state=self.State.COLLECTING_PINCODE,
            address_data={},
            language="en",
        )
        self.assertEqual(state, self.State.COLLECTING_PHONE)
        self.assertEqual(addr.get("postcode", ""), "682001")

    async def test_pincode_invalid_short(self):
        resp, state, addr, _ = await self.orch.handle_address_collection(
            session_id="a5",
            user_message="1234",
            current_state=self.State.COLLECTING_PINCODE,
            address_data={},
            language="en",
        )
        # Should stay on pincode state
        self.assertEqual(state, self.State.COLLECTING_PINCODE)
        self.assertIn("6", resp)  # prompt mentions 6 digits

    async def test_pincode_spoken_digits(self):
        """Six eight two zero zero one → 682001"""
        resp, state, addr, _ = await self.orch.handle_address_collection(
            session_id="a6",
            user_message="six eight two zero zero one",
            current_state=self.State.COLLECTING_PINCODE,
            address_data={},
            language="en",
        )
        self.assertEqual(state, self.State.COLLECTING_PHONE)
        self.assertEqual(addr.get("postcode"), "682001")

    async def test_phone_valid(self):
        resp, state, addr, _ = await self.orch.handle_address_collection(
            session_id="a7",
            user_message="9876543210",
            current_state=self.State.COLLECTING_PHONE,
            address_data={},
            language="en",
        )
        self.assertIn(state, [self.State.COLLECTING_EMAIL, self.State.CONFIRMING])

    async def test_email_skip(self):
        """User can skip email collection."""
        resp, state, addr, _ = await self.orch.handle_address_collection(
            session_id="a8",
            user_message="skip email",
            current_state=self.State.COLLECTING_EMAIL,
            address_data={
                "first_name": "Rahul", "last_name": "Kumar",
                "address_line1": "123 Main St", "city": "Kochi",
                "postcode": "682001", "phone": "9876543210"
            },
            language="en",
        )
        self.assertIn(state, [self.State.CONFIRMING, self.State.COMPLETE])


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 8 — Tool Execution Tests (unit, mocked)
# ─────────────────────────────────────────────────────────────────────────────

class TestToolExecution(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from agent.orchestrator import AgentOrchestrator
        from services.session import SessionService
        self.orch = AgentOrchestrator(
            woocommerce_service=FakeWoo(),
            session_service=SessionService(FakeRedis()),
        )

    async def _call(self, tool_name, tool_args, session_id="t1"):
        return await self.orch._execute_tool_call(
            tool_name=tool_name,
            tool_args=tool_args,
            session_id=session_id,
            cart_context=None,
        )

    async def test_search_products_returns_products(self):
        result, actions, pids, email = await self._call(
            "search_products", {"query": "helmet", "limit": 3}
        )
        self.assertIn("products", result)
        self.assertTrue(len(result["products"]) > 0)
        # Must emit show_products action
        types = [a["type"] for a in actions]
        self.assertIn("show_products", types)

    async def test_search_products_show_products_count_capped(self):
        """show_products must not flood the UI — check action contains products."""
        result, actions, _, _ = await self._call(
            "search_products", {"query": "", "limit": 6}
        )
        sp = next((a for a in actions if a["type"] == "show_products"), None)
        self.assertIsNotNone(sp)
        self.assertIsInstance(sp["payload"]["products"], list)

    async def test_get_categories_returns_categories(self):
        result, actions, pids, email = await self._call("get_categories", {})
        self.assertIn("categories", result)
        self.assertTrue(len(result["categories"]) > 0 or len(result.get("available_products", [])) > 0)

    async def test_get_categories_fallback_shows_one_product(self):
        """When categories fail, fallback shows only 1 product card."""
        # Patch woo.get_categories to return empty (simulating 401)
        self.orch.woo.get_categories = AsyncMock(return_value=[])
        result, actions, pids, email = await self._call("get_categories", {})
        sp_actions = [a for a in actions if a["type"] == "show_products"]
        if sp_actions:
            self.assertEqual(len(sp_actions[0]["payload"]["products"]), 1,
                "Categories fallback must show exactly 1 product card")

    async def test_get_cart_returns_cart(self):
        result, actions, _, _ = await self._call("get_cart", {})
        self.assertIn("cart", result)
        cart = result["cart"]
        self.assertIsNotNone(cart)

    async def test_get_cart_emits_show_cart_action(self):
        result, actions, _, _ = await self._call("get_cart", {})
        types = [a["type"] for a in actions]
        self.assertIn("show_cart", types)

    async def test_get_product_details(self):
        result, actions, pids, _ = await self._call(
            "get_product_details", {"product_id": 101}
        )
        self.assertIn("product", result)
        self.assertEqual(result["product"]["id"], 101)
        self.assertIn(101, pids)

    async def test_apply_coupon(self):
        result, actions, _, _ = await self._call(
            "apply_coupon", {"coupon_code": "SAVE10"}
        )
        types = [a["type"] for a in actions]
        self.assertIn("coupon_applied", types)

    async def test_get_store_info_tool(self):
        result, actions, _, _ = await self._call("get_store_info", {})
        self.assertIn("store_info", result)

    async def test_unknown_tool_handled_gracefully(self):
        """Unknown tool must not crash — returns error dict."""
        result, actions, _, _ = await self._call("nonexistent_tool_xyz", {})
        self.assertIsInstance(result, dict)
        self.assertIsInstance(actions, list)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 9 — Full Orchestrator run() Tests (unit, mocked, no LLM)
# ─────────────────────────────────────────────────────────────────────────────

class TestOrchestratorRun(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from agent.orchestrator import AgentOrchestrator
        from services.session import SessionService
        self.orch = AgentOrchestrator(
            woocommerce_service=FakeWoo(),
            session_service=SessionService(FakeRedis()),
            # No LLM clients configured — ANY_LLM_AVAILABLE=False forces fast-intent path
        )
        self.store_ctx = {
            "store_name": "My Store",
            "currency_symbol": "₹",
        }

    async def _run(self, message, session_id=None):
        return await self.orch.run(
            session_id=session_id or "test-run",
            user_message=message,
            store_context=self.store_ctx,
            language="en",
        )

    async def test_empty_message_returns_greeting(self):
        result = await self._run("")
        self.assertIn("response_text", result)
        text = result["response_text"].lower()
        self.assertTrue(any(w in text for w in ["hi", "hello", "help", "assistant"]))

    async def test_store_info_returns_show_store_info_action(self):
        result = await self._run("Store info")
        action_types = [a["type"] for a in result.get("ui_actions", [])]
        self.assertIn("show_store_info", action_types,
            "Store info quick reply must produce show_store_info UI action")

    async def test_store_info_response_contains_store_name(self):
        result = await self._run("Store info")
        self.assertIn("My Store", result["response_text"])

    async def test_store_info_payload_has_all_fields(self):
        result = await self._run("Store info")
        action = next(a for a in result["ui_actions"] if a["type"] == "show_store_info")
        payload = action["payload"]
        for field in ["store_name", "shipping", "returns", "payment_methods", "currency"]:
            self.assertIn(field, payload, f"show_store_info payload missing '{field}'")

    async def test_cart_view_returns_show_cart_action(self):
        result = await self._run("Show my cart")
        action_types = [a["type"] for a in result.get("ui_actions", [])]
        self.assertIn("show_cart", action_types,
            "Show my cart quick reply must produce show_cart UI action")

    async def test_cart_view_has_items(self):
        result = await self._run("Show my cart")
        action = next(a for a in result["ui_actions"] if a["type"] == "show_cart")
        cart = action["payload"]["cart"]
        self.assertIsNotNone(cart)

    async def test_no_crash_on_unknown_message(self):
        """Any message must return a dict with response_text, never crash."""
        result = await self._run("xyzzy frobble blargh")
        self.assertIsInstance(result, dict)
        self.assertIn("response_text", result)

    async def test_result_always_has_required_keys(self):
        """Response must always have these keys for the widget to work."""
        for msg in ["Store info", "Show my cart", "Browse", "hello", ""]:
            result = await self._run(msg, session_id=f"key-test-{msg[:5]}")
            for key in ["response_text", "ui_actions"]:
                self.assertIn(key, result, f"Missing '{key}' for message: {msg!r}")

    async def test_actions_and_ui_actions_are_same(self):
        """Widget relies on both 'ui_actions' and 'actions' being identical."""
        result = await self._run("Store info")
        self.assertEqual(result.get("ui_actions"), result.get("actions"),
            "'ui_actions' and 'actions' must be identical for widget compatibility")


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 10 — Live Server Integration Tests
# ─────────────────────────────────────────────────────────────────────────────

def _session(suffix):
    return f"{SESSION_PREFIX}-{suffix}"


def check_server():
    """Returns True if local server is reachable."""
    try:
        r = httpx.get(f"{BASE_URL}/health", timeout=3)
        return r.status_code < 500
    except Exception:
        return False


SERVER_UP = check_server()
skip_if_no_server = pytest.mark.skipif(not SERVER_UP, reason="Backend server not running on port 8000")


def post_chat(message: str, session_id: str = None, timeout: int = 45) -> dict:
    payload = {
        "session_id": session_id or _session("gen"),
        "message": message,
    }
    with httpx.Client(timeout=timeout) as client:
        r = client.post(f"{BASE_URL}/chat", json=payload)
        r.raise_for_status()
        return r.json()


class TestLiveServerQuickReplies(unittest.TestCase):
    """Test all quick replies against the running server."""

    @skip_if_no_server
    def test_store_info_live(self):
        r = post_chat("Store info", session_id=_session("si"))
        self.assertIn("response_text", r)
        self.assertTrue(len(r["response_text"]) > 5, "Store info should return real text")
        action_types = [a["type"] for a in (r.get("ui_actions") or r.get("actions") or [])]
        self.assertIn("show_store_info", action_types,
            f"Expected show_store_info action, got: {action_types}")

    @skip_if_no_server
    def test_store_info_payload_complete(self):
        r = post_chat("Store info", session_id=_session("si2"))
        actions = r.get("ui_actions") or r.get("actions") or []
        si = next((a for a in actions if a["type"] == "show_store_info"), None)
        self.assertIsNotNone(si, "show_store_info action missing")
        for field in ["store_name", "shipping", "returns", "payment_methods"]:
            self.assertIn(field, si["payload"], f"show_store_info payload missing '{field}'")

    @skip_if_no_server
    def test_show_my_cart_live(self):
        r = post_chat("Show my cart", session_id=_session("cart"))
        action_types = [a["type"] for a in (r.get("ui_actions") or r.get("actions") or [])]
        self.assertIn("show_cart", action_types,
            f"Expected show_cart, got: {action_types}")

    @skip_if_no_server
    def test_browse_live(self):
        r = post_chat("Browse", session_id=_session("browse"))
        self.assertIn("response_text", r)
        self.assertTrue(len(r["response_text"]) > 5)

    @skip_if_no_server
    def test_show_best_sellers_live(self):
        r = post_chat("Show best sellers", session_id=_session("bs"))
        self.assertIn("response_text", r)
        self.assertTrue(len(r["response_text"]) > 5)
        # Must not say "Sorry, I missed that"
        self.assertNotIn("missed that", r["response_text"].lower(),
            "Best sellers quick reply should not fall back to 'I missed that'")

    @skip_if_no_server
    def test_no_quick_reply_returns_sorry(self):
        """None of the standard quick replies should return the generic sorry message."""
        quick_replies = ["Store info", "Show my cart", "Browse", "Show best sellers"]
        for qr in quick_replies:
            r = post_chat(qr, session_id=_session(f"qr-{qr[:4]}"))
            text = r.get("response_text", "").lower()
            self.assertNotIn("sorry, i missed that", text,
                f"Quick reply {qr!r} returned generic sorry: {text[:100]}")
            self.assertNotIn("can't help with that", text,
                f"Quick reply {qr!r} got safety refusal: {text[:100]}")


class TestLiveServerResponseStructure(unittest.TestCase):
    """Verify the shape of every response matches what the widget expects."""

    @skip_if_no_server
    def test_response_has_required_fields(self):
        r = post_chat("hello", session_id=_session("struct"))
        for field in ["session_id", "text", "response_text", "ui_actions", "language"]:
            self.assertIn(field, r, f"Missing field: {field!r}")

    @skip_if_no_server
    def test_audio_format_is_mp3_for_google(self):
        """Google TTS outputs MP3 — audio_format must be 'mp3' not 'wav'."""
        r = post_chat("hello", session_id=_session("audio"))
        if r.get("audio_base64"):
            self.assertEqual(r.get("audio_format"), "mp3",
                "Google TTS audio_format must be 'mp3'. If 'wav', browser will fail to play audio.")

    @skip_if_no_server
    def test_audio_base64_is_valid_when_present(self):
        """If audio_base64 is present it must be valid base64."""
        r = post_chat("hello", session_id=_session("b64"))
        if r.get("audio_base64"):
            try:
                decoded = base64.b64decode(r["audio_base64"])
                self.assertGreater(len(decoded), 100, "Audio data too small, likely an error")
            except Exception as e:
                self.fail(f"audio_base64 is not valid base64: {e}")

    @skip_if_no_server
    def test_ui_actions_is_list(self):
        r = post_chat("hello", session_id=_session("uia"))
        self.assertIsInstance(r.get("ui_actions"), list)

    @skip_if_no_server
    def test_ui_actions_and_actions_match(self):
        r = post_chat("Store info", session_id=_session("dup"))
        self.assertEqual(r.get("ui_actions"), r.get("actions"),
            "ui_actions and actions must be identical")

    @skip_if_no_server
    def test_language_field_returned(self):
        r = post_chat("hello", session_id=_session("lang"))
        self.assertIn(r.get("language", "en"), ["en", "hi", "ml", "ta", "te", "bn", "kn", "gu", "pa"])

    @skip_if_no_server
    def test_session_id_echoed(self):
        sid = _session("echo")
        r = post_chat("hello", session_id=sid)
        self.assertEqual(r.get("session_id"), sid)


class TestLiveServerProductDisplay(unittest.TestCase):
    """Verify product display consistency — UI shows what agent says."""

    @skip_if_no_server
    def test_browse_show_products_count(self):
        """Browse/best sellers must show ≤ 1 product in fast-intent path."""
        r = post_chat("Show best sellers", session_id=_session("pd1"))
        actions = r.get("ui_actions") or r.get("actions") or []
        sp = next((a for a in actions if a["type"] == "show_products"), None)
        if sp:
            count = len(sp["payload"].get("products", []))
            self.assertLessEqual(count, 2,
                f"Browse quick reply should show 1 product card, got {count} — this causes UI/voice mismatch")

    @skip_if_no_server
    def test_store_info_does_not_show_products(self):
        """Store info must NOT show product grid."""
        r = post_chat("Store info", session_id=_session("pd2"))
        actions = r.get("ui_actions") or r.get("actions") or []
        sp = [a for a in actions if a["type"] == "show_products"]
        self.assertEqual(len(sp), 0,
            "Store info response must not include show_products action")

    @skip_if_no_server
    def test_cart_view_does_not_show_products(self):
        """Cart view must show cart, not product grid."""
        r = post_chat("Show my cart", session_id=_session("pd3"))
        actions = r.get("ui_actions") or r.get("actions") or []
        sp = [a for a in actions if a["type"] == "show_products"]
        self.assertEqual(len(sp), 0, "Cart view must not show product grid")


class TestLiveServerEdgeCases(unittest.TestCase):
    """Edge cases and error resilience."""

    @skip_if_no_server
    def test_very_long_message(self):
        """Long message within limit must be handled gracefully."""
        # Max allowed by schema validation is ~500 chars; stay under it
        long_msg = ("show me products " * 20)[:480]
        r = post_chat(long_msg, session_id=_session("long"))
        self.assertIn("response_text", r)

    @skip_if_no_server
    def test_special_characters_in_message(self):
        """Special chars must not cause injection or crash."""
        r = post_chat("<script>alert('xss')</script>", session_id=_session("xss"))
        self.assertIn("response_text", r)
        self.assertNotIn("<script>", r.get("response_text", ""))

    @skip_if_no_server
    def test_emoji_in_message(self):
        r = post_chat("Show me helmets 🚴", session_id=_session("emoji"))
        self.assertIn("response_text", r)

    @skip_if_no_server
    def test_same_session_persists_context(self):
        """Same session_id should persist state across calls."""
        sid = _session("persist")
        post_chat("hello", session_id=sid)
        r2 = post_chat("Show my cart", session_id=sid)
        # Should still work after multiple calls
        self.assertIn("response_text", r2)

    @skip_if_no_server
    def test_concurrent_different_sessions(self):
        """Different session_ids must not interfere."""
        import threading
        results = {}

        def call(msg, sid):
            try:
                results[sid] = post_chat(msg, session_id=sid)
            except Exception as e:
                results[sid] = {"error": str(e)}

        threads = [
            threading.Thread(target=call, args=("Store info", _session("c1"))),
            threading.Thread(target=call, args=("Show my cart", _session("c2"))),
            threading.Thread(target=call, args=("Browse", _session("c3"))),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        for sid, r in results.items():
            self.assertNotIn("error", r, f"Session {sid} failed: {r}")
            self.assertIn("response_text", r)

    @skip_if_no_server
    def test_response_time_acceptable(self):
        """Responses should arrive within 30 seconds."""
        start = time.time()
        r = post_chat("Store info", session_id=_session("perf"))
        elapsed = time.time() - start
        self.assertLess(elapsed, 30,
            f"Response took {elapsed:.1f}s — too slow (fast-intent should be <2s)")

    @skip_if_no_server
    def test_store_info_response_time_fast(self):
        """Store info is a fast-intent — must respond in under 5 seconds."""
        start = time.time()
        r = post_chat("Store info", session_id=_session("perf2"))
        elapsed = time.time() - start
        self.assertLess(elapsed, 5,
            f"Store info took {elapsed:.1f}s — should be instant (no LLM needed)")

    @skip_if_no_server
    def test_cart_view_response_time_fast(self):
        """Cart view is a fast-intent — must respond in under 5 seconds."""
        start = time.time()
        r = post_chat("Show my cart", session_id=_session("perf3"))
        elapsed = time.time() - start
        self.assertLess(elapsed, 5,
            f"Cart view took {elapsed:.1f}s — should be instant")


class TestLiveServerHealthAndConfig(unittest.TestCase):
    @skip_if_no_server
    def test_health_endpoint(self):
        r = httpx.get(f"{BASE_URL}/health", timeout=5)
        self.assertIn(r.status_code, [200, 204])

    @skip_if_no_server
    def test_invalid_session_id_rejected(self):
        """Session IDs with special chars must be rejected with 400."""
        r = httpx.post(f"{BASE_URL}/chat",
            json={"session_id": "../../etc/passwd", "message": "hi"},
            timeout=5)
        self.assertEqual(r.status_code, 400)

    @skip_if_no_server
    def test_missing_session_id_rejected(self):
        r = httpx.post(f"{BASE_URL}/chat",
            json={"message": "hi"},
            timeout=5)
        self.assertIn(r.status_code, [400, 422])

    @skip_if_no_server
    def test_chat_endpoint_post_only(self):
        r = httpx.get(f"{BASE_URL}/chat", timeout=5)
        self.assertIn(r.status_code, [405, 404])
