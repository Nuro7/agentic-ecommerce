"""Tests for the checkout address prefill FSM (save new / fetch existing by phone)."""
import asyncio
from src.app.agent.brain.address import (
    AddressCollectionState as S,
    handle_address_collection,
)
from src.app.agent.brain.text_utils import (
    normalize_province_code,
    normalize_country_code,
)
from src.app.integrations.shopify.client import (
    ShopifyClient,
    _ensure_cart_gid,
)


def call(msg, state, addr_data, page_context, tenant_id="t1"):
    return asyncio.run(
        handle_address_collection(
            session_id="s1", user_message=msg, current_state=state,
            address_data=addr_data, language="en",
            page_context=page_context, tenant_id=tenant_id,
        )
    )


def test_checkout_idle_starts_phone_first():
    resp, next_state, data, actions = call(
        "5123456789", S.IDLE, {}, {"page_type": "checkout", "url": "https://store/checkout"},
    )
    assert next_state == S.COLLECTING_PHONE


def test_existing_customer_fetched_by_phone_and_prefilled(monkeypatch):
    async def fake_get(phone, tenant_id):
        assert tenant_id == "t1"
        return {
            "first_name": "Asha", "last_name": "Nair", "address_1": "Flat 12, MG Road",
            "city": "Kochi", "state": "Kerala", "postcode": "682015",
            "email": "asha@example.com",
        }
    monkeypatch.setattr("src.app.modules.users.address_service.get_address_by_phone", fake_get)

    resp, next_state, addr, actions = call(
        "9876543210", S.COLLECTING_PHONE, {}, {"page_type": "checkout", "url": "https://store/checkout"},
    )
    # Known DB match → fill the saved details, prefill the form, and go to
    # CONFIRMING so the customer verifies BEFORE the checkout redirect.
    assert next_state == S.CONFIRMING
    assert addr["first_name"] == "Asha"
    assert addr["_using_saved"] == "1"
    assert addr["postcode"] == "682015"
    assert any(a.get("type") == "prefill_address" for a in actions)
    assert not any(a.get("type") == "redirect_checkout_with_address" for a in actions)


def test_saved_address_confirm_then_checkout(monkeypatch):
    saved = {}
    async def fake_save(session_id, tenant_id, phone, address_data):
        saved["phone"] = phone
    monkeypatch.setattr("src.app.modules.users.address_service.save_address", fake_save)

    # Returning customer whose phone matches a saved address: first the FSM
    # pre-fills and asks to confirm (no redirect yet)...
    addr_data = {
        "first_name": "Asha", "last_name": "Nair", "address_line1": "Flat 12, MG Road",
        "city": "Kochi", "state": "Kerala", "postcode": "682015",
        "phone": "9876543210", "email": "asha@example.com", "_using_saved": "1",
    }
    resp, next_state, data, actions = call(
        "yes", S.CONFIRMING, addr_data, {"page_type": "checkout", "url": "https://store/checkout"},
    )
    # ...only a confirmed "yes" issues the checkout redirect (address auto-fill).
    assert next_state == S.COMPLETE
    redirect = next((a for a in actions if a.get("type") == "redirect_checkout_with_address"), None)
    assert redirect is not None
    assert redirect["payload"]["shipping"]["state_code"] == "KL"
    assert saved.get("phone") == "9876543210"


def test_spoken_digit_phone_normalized_before_validation(monkeypatch):
    monkeypatch.setattr(
        "src.app.modules.users.address_service.get_address_by_phone",
        lambda phone, tenant: None,
    )
    # Spoken "nine eight seven six five four three two one zero" must be
    # normalized to 9876543210 (>= 10 digits) and NOT rejected as invalid.
    resp, next_state, addr, actions = call(
        "nine eight seven six five four three two one zero", S.COLLECTING_PHONE, {},
        {"page_type": "checkout", "url": "https://store/checkout"},
    )
    assert addr["phone"] == "9876543210"
    assert next_state == S.COLLECTING_NAME


def test_formatted_phone_normalized_before_validation(monkeypatch):
    monkeypatch.setattr(
        "src.app.modules.users.address_service.get_address_by_phone",
        lambda phone, tenant: None,
    )
    resp, next_state, addr, actions = call(
        "+91 987-654-3210", S.COLLECTING_PHONE, {},
        {"page_type": "checkout", "url": "https://store/checkout"},
    )
    assert addr["phone"] == "9876543210"
    assert next_state == S.COLLECTING_NAME


def test_new_customer_saves_address_on_confirm(monkeypatch):
    saved = {}
    async def fake_save(session_id, tenant_id, phone, address_data):
        saved["phone"] = phone
        saved["data"] = address_data
    monkeypatch.setattr("src.app.modules.users.address_service.save_address", fake_save)

    addr = {
        "first_name": "Ravi", "last_name": "Kumar", "address_line1": "12 Park Road",
        "city": "Chennai", "state": "Tamil Nadu", "postcode": "600001", "phone": "9876543210",
    }
    resp, next_state, data, actions = call(
        "yes", S.CONFIRMING, addr, {"page_type": "checkout", "url": "https://store/checkout"},
    )
    assert next_state == S.COMPLETE
    assert saved.get("phone") == "9876543210"
    assert saved["data"]["city"] == "Chennai"
    assert any(a.get("type") == "redirect_checkout_with_address" for a in actions)


def test_unknown_phone_keeps_manual_confirm_on_checkout(monkeypatch):
    monkeypatch.setattr(
        "src.app.modules.users.address_service.get_address_by_phone",
        lambda phone, tenant: None,
    )
    resp, next_state, addr, actions = call(
        "9876543211", S.COLLECTING_PHONE, {},
        {"page_type": "checkout", "url": "https://store/checkout"},
    )
    # No DB match → collect the full address (name first), no redirect yet.
    assert next_state == S.COLLECTING_NAME
    assert addr.get("_using_saved", "") != "1"
    assert not any(a.get("type") == "redirect_checkout_with_address" for a in actions)


# ── ISO-2 normalization helpers ────────────────────────────────────────────────

def test_normalize_province_code_us_full_name():
    assert normalize_province_code("California", "US") == "CA"
    assert normalize_province_code("new york", "US") == "NY"
    assert normalize_province_code("West Bengal", "US") == "West Bengal"


def test_normalize_province_code_india():
    assert normalize_province_code("Maharashtra", "IN") == "MH"
    assert normalize_province_code("Tamil Nadu", "IN") == "TN"
    assert normalize_province_code("KL", "IN") == "KL"


def test_normalize_province_code_bare_and_unknown():
    assert normalize_province_code("ca") == "CA"
    assert normalize_province_code("Bavaria", "DE") == "Bavaria"
    assert normalize_province_code("") == ""


def test_normalize_country_code():
    assert normalize_country_code("India") == "IN"
    assert normalize_country_code("usa") == "US"
    assert normalize_country_code("United Kingdom") == "GB"
    assert normalize_country_code("uk") == "GB"
    assert normalize_country_code("U.S.") == "US"
    assert normalize_country_code("") == "US"
    assert normalize_country_code("", default="IN") == "IN"
    assert normalize_country_code("ca") == "CA"


# ── FSM payloads now carry ISO-2 codes ─────────────────────────────────────────

def test_prefill_payload_emits_iso2_codes():
    addr = {
        "first_name": "Ravi", "last_name": "Kumar", "address_line1": "12 Park Road",
        "city": "Chennai", "state": "Tamil Nadu", "postcode": "600001", "phone": "9876543210",
    }
    resp, next_state, data, actions = call(
        "skip", S.COLLECTING_EMAIL, addr, {"page_type": "checkout", "url": "https://store/checkout"},
    )
    prefill = next((a for a in actions if a.get("type") == "prefill_address"), None)
    assert prefill is not None
    payload = prefill["payload"]
    assert payload["state_code"] == "TN"
    assert payload["country_code"] == "IN"


def test_redirect_payload_emits_iso2_codes(monkeypatch):
    monkeypatch.setattr(
        "src.app.modules.users.address_service.save_address",
        lambda **kw: None,
    )
    addr = {
        "first_name": "Asha", "last_name": "Nair", "address_line1": "Flat 12, MG Road",
        "city": "Kochi", "state": "Kerala", "postcode": "682015", "phone": "9876543210",
    }
    resp, next_state, data, actions = call(
        "yes", S.CONFIRMING, addr, {"page_type": "checkout", "url": "https://store/checkout"},
    )
    redirect = next((a for a in actions if a.get("type") == "redirect_checkout_with_address"), None)
    assert redirect is not None
    shipping = redirect["payload"]["shipping"]
    assert shipping["state_code"] == "KL"
    assert shipping["country_code"] == "IN"


# ── cartDeliveryAddressesReplace client path ───────────────────────────────────

def test_ensure_cart_gid():
    assert _ensure_cart_gid("c1-abc123") == "gid://shopify/Cart/c1-abc123"
    assert _ensure_cart_gid("gid://shopify/Cart/c1-abc123") == "gid://shopify/Cart/c1-abc123"
    assert _ensure_cart_gid("") == ""


def _new_client():
    return ShopifyClient(store_domain="test.myshopify.com", storefront_token="tk")


def test_replace_cart_delivery_address_sends_modern_mutation(monkeypatch):
    captured = {}

    async def fake_storefront(query, variables):
        captured["query"] = query
        captured["variables"] = variables
        return {
            "cartDeliveryAddressesReplace": {
                "cart": {
                    "id": "gid://shopify/Cart/c1-abc",
                    "checkoutUrl": "https://checkout.test",
                },
                "userErrors": [],
            }
        }

    client = _new_client()
    monkeypatch.setattr(client, "_storefront", fake_storefront)
    try:
        result = asyncio.run(client.replace_cart_delivery_address(
            cart_id="c1-abc",
            address={
                "first_name": "Asha", "last_name": "Nair", "address_1": "Flat 12, MG Road",
                "city": "Kochi", "state": "Kerala", "state_code": "KL", "country_code": "IN",
                "postcode": "682015", "phone": "9876543210",
            },
        ))
    finally:
        asyncio.run(client._http.aclose())

    assert "cartDeliveryAddressesReplace" in captured["query"]
    assert "deliveryAddressPreferences" not in captured["query"]
    assert captured["variables"]["cartId"] == "gid://shopify/Cart/c1-abc"
    assert captured["variables"]["addresses"][0]["oneTimeUse"] is True
    assert captured["variables"]["addresses"][0]["address"]["province"] == "KL"
    assert captured["variables"]["addresses"][0]["address"]["country"] == "IN"
    assert result["success"] is True
    assert result["checkout_url"] == "https://checkout.test"


def test_replace_cart_delivery_address_falls_back_on_old_api(monkeypatch):
    calls = []
    captured = {}

    async def fake_storefront(query, variables):
        calls.append(query)
        if len(calls) == 1:
            raise RuntimeError(
                "Shopify Storefront error: [{'message': \"Field 'cartDeliveryAddressesReplace' doesn't exist on type 'Mutation'\"}]"
            )
        captured["variables"] = variables
        return {
            "cartBuyerIdentityUpdate": {
                "cart": {"id": "gid://shopify/Cart/c1-abc", "checkoutUrl": "https://checkout.test"},
                "userErrors": [],
            }
        }

    client = _new_client()
    monkeypatch.setattr(client, "_storefront", fake_storefront)
    try:
        result = asyncio.run(client.replace_cart_delivery_address(
            cart_id="gid://shopify/Cart/c1-abc",
            address={
                "first_name": "Asha", "last_name": "Nair", "address_1": "Flat 12, MG Road",
                "city": "Kochi", "state": "Kerala", "state_code": "KL", "country_code": "IN",
                "postcode": "682015", "phone": "9876543210",
            },
        ))
    finally:
        asyncio.run(client._http.aclose())

    assert len(calls) == 2
    assert "cartDeliveryAddressesReplace" in calls[0]
    assert "cartBuyerIdentityUpdate" in calls[1]
    legacy_addr = captured["variables"]["buyerIdentity"]["deliveryAddressPreferences"][0]["deliveryAddress"]
    assert legacy_addr["province"] == "KL"
    assert legacy_addr["country"] == "IN"
    assert result["success"] is True
    assert result["checkout_url"] == "https://checkout.test"


def test_replace_cart_delivery_address_empty_when_both_paths_fail(monkeypatch):
    async def fake_storefront(query, variables):
        raise RuntimeError("Shopify Storefront error: connection refused")

    client = _new_client()
    monkeypatch.setattr(client, "_storefront", fake_storefront)
    try:
        result = asyncio.run(client.replace_cart_delivery_address(
            cart_id="gid://shopify/Cart/c1-abc",
            address={"first_name": "Asha", "city": "Kochi"},
        ))
    finally:
        asyncio.run(client._http.aclose())

    assert result.get("success") is not True
    assert result.get("checkout_url") == ""
    assert result.get("is_empty") is True


def test_checkout_intent_does_not_emit_redirect_while_collecting(monkeypatch):
    """Regression: 'proceed to checkout' from a cart page must start phone
    collection WITHOUT a /checkout redirect.

    The generic append_live_navigation post-processor used to match the word
    "checkout" and append a plain redirect next to the FSM's phone prompt,
    navigating the customer to checkout before any address was collected.
    When the address FSM owns the turn, live-navigation must be skipped and the
    FSM alone decides checkout navigation (prefill_address then, on confirm,
    redirect_checkout_with_address).
    """
    from src.app.agent.brain import core as brain_core
    from src.app.agent.classifier import IntentResult

    nav_calls = []

    async def fake_append_live_navigation(*args, **kwargs):
        nav_calls.append((args, kwargs))

    class FakeClassifier:
        async def classify(self, message, lang):
            return IntentResult(intent="checkout", confidence=0.9, via="test")

    class FakeSession:
        def __init__(self):
            self.meta = {"language": "en"}
            self.session = {}

        async def get_meta(self, tenant_id, session_id):
            return dict(self.meta)

        async def save_meta(self, tenant_id, session_id, meta):
            self.meta = dict(meta)
            return None

        async def get_session(self, tenant_id, session_id):
            return dict(self.session)

        async def get_cart(self, tenant_id, session_id):
            return None

        async def save_cart(self, tenant_id, session_id, cart):
            return None

        async def update_session(self, tenant_id, session_id, **kwargs):
            return None

    class FakeFacts:
        async def get(self, tenant_id, session_id):
            return {}

        async def update(self, tenant_id, session_id, message, facts_payload):
            return None

    class FakeBeta:
        async def record_turn(self, **kwargs):
            return None

    monkeypatch.setattr(brain_core, "append_live_navigation", fake_append_live_navigation)
    monkeypatch.setattr(brain_core, "get_classifier", lambda: FakeClassifier())
    monkeypatch.setattr(brain_core, "get_session_facts_service", lambda *a, **k: FakeFacts())
    monkeypatch.setattr(brain_core, "get_beta_logger", lambda: FakeBeta())

    result = asyncio.run(brain_core.ask_brain(
        session_id="sess_nav_test",
        user_message="proceed to checkout",
        store_context={"url": "https://speako-demo.com"},
        page_context={"url": "https://speako-demo.com/cart"},
        language="en",
        store_client=object(),
        session_service=FakeSession(),
        redis=None,
        db_session_factory=None,
    ))

    types = [a.get("type") for a in (result.get("ui_actions") or [])]
    assert "redirect" not in types
    assert "redirect_checkout" not in types
    assert "redirect_checkout_with_address" not in types
    assert "checkout" not in types
    assert nav_calls == [], "append_live_navigation must not run on an FSM-owned turn"
    assert "phone number" in (result.get("response_text") or "").lower()
