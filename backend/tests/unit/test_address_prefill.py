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
        "city": "Kochi", "state": "California", "postcode": "90001",
        "phone": "9876543210", "email": "asha@example.com", "_using_saved": "1",
    }
    resp, next_state, data, actions = call(
        "yes", S.CONFIRMING, addr_data, {"page_type": "checkout", "url": "https://store/checkout"},
    )
    # ...only a confirmed "yes" issues the checkout redirect (address auto-fill).
    assert next_state == S.COMPLETE
    redirect = next((a for a in actions if a.get("type") == "redirect_checkout_with_address"), None)
    assert redirect is not None
    assert redirect["payload"]["shipping"]["state_code"] == "CA"
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


def test_us_zip_five_digits_accept(monkeypatch):
    """US ZIP codes are 5 digits (not 6 like India). A 5-digit ZIP must be
    accepted and preserved through the flow."""
    monkeypatch.setattr(
        "src.app.modules.users.address_service.save_address",
        lambda **kw: None,
    )
    resp, next_state, data, actions = call(
        "90001", S.COLLECTING_PINCODE,
        {"first_name": "Asha", "last_name": "Nair", "phone": "9876543210"},
        {"page_type": "checkout", "url": "https://store/checkout"},
    )
    assert data["postcode"] == "90001"
    assert next_state == S.COLLECTING_EMAIL


def test_phone_optional_skip_proceeds(monkeypatch):
    """Phone number is optional — saying 'skip' must not block the checkout
    flow; the FSM proceeds to collect the name instead."""
    monkeypatch.setattr(
        "src.app.modules.users.address_service.get_address_by_phone",
        lambda phone, tenant: None,
    )
    resp, next_state, addr, actions = call(
        "skip", S.COLLECTING_PHONE, {},
        {"page_type": "checkout", "url": "https://store/checkout"},
    )
    assert addr["phone"] == ""
    assert next_state == S.COLLECTING_NAME
    assert not any(a.get("type") == "redirect_checkout_with_address" for a in actions)


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


def test_complete_state_reissues_redirect_on_second_checkout():
    """Regression: after the FSM COMPLETES, a second 'proceed to checkout' (e.g.
    from the cart / product page) must RE-ISSUE the checkout redirect using the
    stored address — NOT return an empty response, which made the voice model
    hallucinate the navigation while nothing happened."""
    addr = {
        "first_name": "Asha", "last_name": "Nair", "address_line1": "Flat 12, MG Road",
        "city": "Kochi", "state": "Kerala", "postcode": "682015", "phone": "9876543210",
    }
    resp, next_state, data, actions = call(
        "proceed to checkout", S.COMPLETE, addr,
        {"page_type": "cart", "url": "https://store/cart"},
    )
    redirect = next((a for a in actions if a.get("type") == "redirect_checkout_with_address"), None)
    assert redirect is not None, "COMPLETE re-entry must re-emit the redirect"
    assert redirect["payload"]["shipping"]["address_1"] == "Flat 12, MG Road"
    assert resp  # non-empty spoken response so the voice model has real text


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
        "city": "Los Angeles", "state": "California", "postcode": "90001", "phone": "9876543210",
    }
    resp, next_state, data, actions = call(
        "skip", S.COLLECTING_EMAIL, addr, {"page_type": "checkout", "url": "https://store/checkout"},
    )
    prefill = next((a for a in actions if a.get("type") == "prefill_address"), None)
    assert prefill is not None
    payload = prefill["payload"]
    assert payload["state_code"] == "CA"
    assert payload["country_code"] == "US"


def test_redirect_payload_emits_iso2_codes(monkeypatch):
    monkeypatch.setattr(
        "src.app.modules.users.address_service.save_address",
        lambda **kw: None,
    )
    addr = {
        "first_name": "Asha", "last_name": "Nair", "address_line1": "Flat 12, MG Road",
        "city": "Kochi", "state": "California", "postcode": "90001", "phone": "9876543210",
    }
    resp, next_state, data, actions = call(
        "yes", S.CONFIRMING, addr, {"page_type": "checkout", "url": "https://store/checkout"},
    )
    redirect = next((a for a in actions if a.get("type") == "redirect_checkout_with_address"), None)
    assert redirect is not None
    shipping = redirect["payload"]["shipping"]
    assert shipping["state_code"] == "CA"
    assert shipping["country_code"] == "US"


# ── cartDeliveryAddressesReplace client path ───────────────────────────────────

def test_ensure_cart_gid():
    assert _ensure_cart_gid("c1-abc123") == "gid://shopify/Cart/c1-abc123"
    assert _ensure_cart_gid("gid://shopify/Cart/c1-abc123") == "gid://shopify/Cart/c1-abc123"
    assert _ensure_cart_gid("") == ""


def _new_client():
    return ShopifyClient(store_domain="test.myshopify.com", storefront_token="tk")


def test_replace_cart_delivery_address_sends_modern_mutation(monkeypatch):
    captured = {}

    async def fake_storefront(query, variables, *, buyer_ip=None):
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
    assert captured["variables"]["addresses"][0]["selected"] is True
    delivery = captured["variables"]["addresses"][0]["address"]["deliveryAddress"]
    assert delivery["provinceCode"] == "KL"
    assert delivery["countryCode"] == "IN"
    assert result["success"] is True
    assert result["checkout_url"] == "https://checkout.test"


def test_replace_cart_delivery_address_reports_failure_on_old_api(monkeypatch):
    # Shopify's hosted checkout never prefills from the legacy
    # deliveryAddressPreferences path (it returns a checkoutUrl yet leaves the
    # delivery form blank). So when the modern cartDeliveryAddressesReplace
    # mutation is unavailable (old API) the bind must FAIL LOUD — not silently
    # "succeed" via the legacy path — so the widget never ships the customer to
    # an empty checkout. (Bug: silent legacy fallback reported success + a
    # checkout_url while the form stayed blank.)
    calls = []

    async def fake_storefront(query, variables, *, buyer_ip=None):
        calls.append(query)
        raise RuntimeError(
            "Shopify Storefront error: [{message: \"Field 'cartDeliveryAddressesReplace' doesn't exist on type 'Mutation'\"}]"
        )

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

    assert len(calls) == 1
    assert "cartDeliveryAddressesReplace" in calls[0]
    # Must NOT fall back to the (non-prefilling) legacy path.
    assert not any("cartBuyerIdentityUpdate" in c for c in calls)
    assert result.get("success") is False
    assert result.get("checkout_url") == ""


def test_replace_cart_delivery_address_empty_when_both_paths_fail(monkeypatch):
    async def fake_storefront(query, variables, *, buyer_ip=None):
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


def test_active_phone_flow_routes_digits_to_fsm_not_search(monkeypatch):
    """Regression: once the address FSM is collecting the phone, a bare phone
    number on the NEXT turn must be routed back to the FSM (→ ask for the name),
    NOT fall through to product search ("I could not find any products").

    The old gate required THIS turn to look like checkout (page_type/URL or a
    checkout phrase), but a phone number matches neither, so the digits were
    classified as a product search. Fix: an active flow owns the turn.
    """
    from src.app.agent.brain import core as brain_core
    from src.app.agent.classifier import IntentResult

    nav_calls = []

    async def fake_append_live_navigation(*args, **kwargs):
        nav_calls.append((args, kwargs))

    class FakeClassifier:
        async def classify(self, message, lang):
            return IntentResult(intent="search", confidence=0.9, via="test")

    class FakeSession:
        def __init__(self):
            self.meta = {
                "language": "en",
                "address_state": "collecting_phone",
                "address_data": {},
            }
            self.session = {}

        async def get_meta(self, tenant_id, session_id):
            return dict(self.meta)

        async def save_meta(self, tenant_id, session_id, meta):
            self.meta = {**self.meta, **meta}
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
        session_id="sess_phone_flow",
        user_message="9876543210",
        store_context={"url": "https://speako-demo.com"},
        page_context={"url": "https://speako-demo.com/product/shoe", "page_type": "product"},
        language="en",
        store_client=object(),
        session_service=FakeSession(),
        redis=None,
        db_session_factory=None,
    ))

    # FSM consumed the digits: asks for the name, never searches products.
    assert "could not find" not in (result.get("response_text") or "").lower()
    assert "name" in (result.get("response_text") or "").lower()
    assert nav_calls == [], "append_live_navigation must not run on an FSM-owned turn"
    assert "redirect" not in [a.get("type") for a in (result.get("ui_actions") or [])]


# ── add_to_cart stale-variant self-heal ───────────────────────────────────────

def _variant_gid(num):
    return f"gid://shopify/ProductVariant/{num}"


def test_add_to_cart_self_heals_stale_variant(monkeypatch):
    """A stale variant id from a cached cart snapshot must not fail checkout:
    on "does not exist", add_to_cart resolves the current purchasable variant
    and retries (matches the production cart/checkout failure)."""
    client = _new_client()
    calls = []

    async def fake_storefront(query, variables, *, buyer_ip=None):
        calls.append(variables)
        if len(calls) == 1:
            return {
                "cartCreate": {
                    "userErrors": [{
                        "field": ["input", "lines", "0", "merchandiseId"],
                        "message": f"The merchandise with id {_variant_gid(10107682521328)} does not exist.",
                    }]
                }
            }
        return {
            "cartCreate": {
                "cart": {
                    "id": "gid://shopify/Cart/c1-abc",
                    "checkoutUrl": "https://checkout.test",
                    "cost": {"totalAmount": {"amount": "10.00", "currencyCode": "USD"}},
                    "lines": {"edges": [{"node": {
                        "id": "gid://shopify/CartLine/l1",
                        "quantity": 1,
                        "merchandise": {"id": _variant_gid(777), "title": "Tee", "product": {"id": "gid://shopify/Product/9", "title": "Tee"}, "image": {"url": ""}},
                        "cost": {"amountPerQuantity": {"amount": "10.00"}, "subtotalAmount": {"amount": "10.00"}},
                    }}]},
                },
                "userErrors": [],
            }
        }

    async def fake_get_details(product_id):
        return {
            "variations": [
                {"id": 10107682521328, "stock_status": "outofstock"},
                {"id": 777, "stock_status": "instock", "attributes": {}},
            ],
            "variations_summary": [],
        }

    monkeypatch.setattr(client, "_storefront", fake_storefront)
    monkeypatch.setattr(client, "get_product_details", fake_get_details)
    monkeypatch.setattr(client, "_cache_delete", lambda k: _noop())
    try:
        result = asyncio.run(client.add_to_cart(
            session_id="s1",
            product_id=9,
            variation_id=10107682521328,
            quantity=1,
        ))
    finally:
        asyncio.run(client._http.aclose())

    assert len(calls) == 2
    assert calls[1]["lines"][0]["merchandiseId"] == _variant_gid(777)
    assert result["success"] is True
    assert result["checkout_url"] == "https://checkout.test"


# ── prepare_checkout per-line fault tolerance ─────────────────────────────────

class _MockRequest:
    def __init__(self, headers=None, host="127.0.0.1"):
        self.headers = headers or {}
        self.client = type("C", (), {"host": host})()

    def _client_ip(self):  # pragma: no cover - placeholder
        return "127.0.0.1"


def test_prepare_checkout_skips_bad_line_and_still_binds(monkeypatch):
    """A single stale line must not abort checkout: prepare_checkout skips it
    and still binds the address to the remaining cart."""
    from src.app.api.v1 import public as public_mod
    from src.app.api.v1.public import PrepareCheckoutRequest

    add_calls = []

    class FakeStoreClient:
        async def add_to_cart(self, **kwargs):
            add_calls.append(kwargs)
            if kwargs.get("variation_id") == 10107682521328:
                raise RuntimeError("merchandise does not exist")
            return {
                "item_count": 1, "is_empty": False,
                "total": "10.00", "checkout_url": "https://checkout.test",
            }

        async def attach_buyer_identity(self, **kwargs):
            return {
                "item_count": 1, "is_empty": False,
                "total": "10.00", "checkout_url": "https://checkout.test",
                "success": True,
            }

    fake_client = FakeStoreClient()
    monkeypatch.setattr(public_mod.os, "getenv", lambda k, d="": {"STORE_COUNTRY": "US"}.get(k, d))

    payload = PrepareCheckoutRequest(
        session_id="s1",
        lines=[
            {"product_id": 9, "variant_id": 10107682521328, "quantity": 1},
            {"product_id": 10, "variant_id": 777, "quantity": 1},
        ],
        email="a@b.co",
        phone="9876543210",
        address={"first_name": "Asha", "last_name": "Nair", "address_1": "MG Road", "city": "Kochi", "state": "Kerala", "postcode": "682015"},
    )

    result = asyncio.run(public_mod.prepare_checkout(payload, _MockRequest(), fake_client, _rl=lambda: None))

    assert [c["variation_id"] for c in add_calls] == [10107682521328, 777]
    assert result["ok"] is True
    assert result["checkout_url"] == "https://checkout.test"
    assert result["bound"] is True


def test_prepare_checkout_fails_when_all_lines_bad(monkeypatch):
    from src.app.api.v1 import public as public_mod
    from src.app.api.v1.public import PrepareCheckoutRequest

    class FakeStoreClient:
        async def add_to_cart(self, **kwargs):
            raise RuntimeError("merchandise does not exist")

        async def attach_buyer_identity(self, **kwargs):
            return {"item_count": 0, "is_empty": True, "total": "0", "checkout_url": ""}

    fake_client = FakeStoreClient()
    monkeypatch.setattr(public_mod.os, "getenv", lambda k, d="": {"STORE_COUNTRY": "US"}.get(k, d))

    payload = PrepareCheckoutRequest(
        session_id="s1",
        lines=[{"product_id": 9, "variant_id": 10107682521328, "quantity": 1}],
        email="a@b.co",
        phone="9876543210",
        address={"first_name": "Asha", "address_1": "MG Road", "city": "Kochi", "state": "Kerala", "postcode": "682015"},
    )

    result = asyncio.run(public_mod.prepare_checkout(payload, _MockRequest(), fake_client, _rl=lambda: None))

    assert result["ok"] is False
    assert result["error"] == "cart_build_failed"
    assert result["checkout_url"] == ""


def test_prepare_checkout_no_checkout_url_when_bind_fails(monkeypatch):
    """A bind that does not set success must NOT return a checkout_url — otherwise
    the widget would navigate to a Storefront cart that carries no address and the
    hosted checkout would open blank (Bug A)."""
    from src.app.api.v1 import public as public_mod
    from src.app.api.v1.public import PrepareCheckoutRequest

    class FakeStoreClient:
        async def add_to_cart(self, **kwargs):
            return {
                "item_count": 1, "is_empty": False,
                "total": "10.00", "checkout_url": "https://checkout.test",
            }

        async def attach_buyer_identity(self, **kwargs):
            # Production returns an empty cart (no success) when the Storefront
            # bind fails/times out.
            return {"item_count": 0, "is_empty": True, "total": "0", "checkout_url": ""}

    fake_client = FakeStoreClient()
    monkeypatch.setattr(public_mod.os, "getenv", lambda k, d="": {"STORE_COUNTRY": "US"}.get(k, d))

    payload = PrepareCheckoutRequest(
        session_id="s1",
        lines=[{"product_id": 9, "variant_id": 101, "quantity": 1}],
        email="a@b.co",
        phone="9876543210",
        address={"first_name": "Asha", "address_1": "MG Road", "city": "Kochi", "state": "Kerala", "postcode": "682015"},
    )

    result = asyncio.run(public_mod.prepare_checkout(payload, _MockRequest(), fake_client, _rl=lambda: None))

    assert result["ok"] is False
    assert result["bound"] is False
    assert result["checkout_url"] == ""


def test_attach_buyer_identity_fails_when_address_bind_fails_even_if_email_ok(monkeypatch):
    """Regression: a failed address bind must NOT be masked by a succeeding
    email/phone cartBuyerIdentityUpdate. Previously the second update overwrote
    `result` with success=True + a checkout_url, so the widget navigated to a
    hosted checkout whose delivery form was blank while the API claimed success."""
    calls = []
    captured = {}

    async def fake_storefront(query, variables, *, buyer_ip=None):
        calls.append(query)
        if "cartDeliveryAddressesReplace" in query:
            captured["addr"] = variables
            # Modern address mutation unavailable on this store → must FAIL LOUD.
            raise RuntimeError(
                "Shopify Storefront error: [{message: \"Field 'cartDeliveryAddressesReplace' doesn't exist on type 'Mutation'\"}]"
            )
        if "cartBuyerIdentityUpdate" in query:
            captured["bi"] = variables
            return {
                "cartBuyerIdentityUpdate": {
                    "cart": {"id": "gid://shopify/Cart/c1-abc", "checkoutUrl": "https://checkout.test"},
                    "userErrors": [],
                }
            }
        raise AssertionError(f"unexpected query: {query[:80]}")

    client = _new_client()
    monkeypatch.setattr(client, "_storefront", fake_storefront)
    async def fake_get_cart_id(session_id):
        return "c1-abc"
    monkeypatch.setattr(client, "_get_cart_id", fake_get_cart_id)
    try:
        result = asyncio.run(client.attach_buyer_identity(
            session_id="s1",
            email="a@b.co",
            phone="9876543210",
            address={"first_name": "Asha", "address_1": "MG Road", "city": "Kochi",
                     "state": "Kerala", "state_code": "KL", "country_code": "IN", "postcode": "682015"},
        ))
    finally:
        asyncio.run(client._http.aclose())

    assert result.get("success") is False
    assert result.get("checkout_url") == ""


def test_get_cart_id_handles_redis_decode_responses():
    """Regression: redis configured with decode_responses=True returns a str (not
    bytes) from .get(). The old `raw.decode()` raised AttributeError → every cart
    id read returned None → the checkout bind failed silently BEFORE the address
    mutation ran (BIND=FAILED reason=- in prod, cart never bound)."""
    class FakeRedis:
        def __init__(self, value):
            self._value = value
        async def get(self, key):
            return self._value

    client = _new_client()
    client.redis = FakeRedis("gid://shopify/Cart/c1-abc")  # str (decode_responses=True)
    result_str = asyncio.run(client._get_cart_id("s1"))
    assert result_str == "gid://shopify/Cart/c1-abc"

    client.redis = FakeRedis(b"gid://shopify/Cart/c1-xyz")  # bytes (decode_responses=False)
    result_bytes = asyncio.run(client._get_cart_id("s1"))
    assert result_bytes == "gid://shopify/Cart/c1-xyz"

    client.redis = FakeRedis(None)
    assert asyncio.run(client._get_cart_id("s1")) is None

    asyncio.run(client._http.aclose())


def test_name_collection_strips_conversational_filler():
    """Regression: voice utterance 'my name is John Doe' must yield
    first_name='John', last_name='Doe' — NOT first='My', last='name is John Doe'.
    The naive first-space split produced a corrupted GraphQL payload name."""
    resp, next_state, addr, actions = call(
        "My name is John Doe", S.COLLECTING_NAME, {}, {},
    )
    assert addr["first_name"] == "John"
    assert addr["last_name"] == "Doe"
    assert next_state == S.COLLECTING_ADDRESS_LINE1


def test_name_collection_strips_i_am_filler():
    resp, next_state, addr, actions = call(
        "I am Asha Nair", S.COLLECTING_NAME, {}, {},
    )
    assert addr["first_name"] == "Asha"
    assert addr["last_name"] == "Nair"
    assert next_state == S.COLLECTING_ADDRESS_LINE1


def test_last_name_collection_strips_filler():
    resp, next_state, addr, actions = call(
        "My name is Doe", S.COLLECTING_LAST_NAME, {}, {},
    )
    assert addr["last_name"] == "Doe"
    assert next_state == S.COLLECTING_ADDRESS_LINE1


def test_attach_buyer_identity_sends_buyer_ip_header(monkeypatch):
    """The Shopify-Storefront-Buyer-IP header must be forwarded on the address
    bind so Shopify's bot mitigation doesn't flag the backend container (seen as
    a reCAPTCHA-protection rejection on the hosted checkout)."""
    captured = {}

    async def fake_storefront(query, variables, *, buyer_ip=None):
        captured["buyer_ip"] = buyer_ip
        return {
            "cartDeliveryAddressesReplace": {
                "cart": {"id": "gid://shopify/Cart/c1-abc", "checkoutUrl": "https://checkout.test"},
                "userErrors": [],
            }
        }

    client = _new_client()
    monkeypatch.setattr(client, "_storefront", fake_storefront)
    async def fake_get_cart_id(session_id):
        return "c1-abc"
    monkeypatch.setattr(client, "_get_cart_id", fake_get_cart_id)
    try:
        result = asyncio.run(client.attach_buyer_identity(
            session_id="s1",
            email="a@b.co",
            address={"first_name": "John", "address_1": "123 Main Street", "city": "NYC",
                     "state_code": "NY", "country_code": "US", "postcode": "10001"},
            buyer_ip="203.0.113.9",
        ))
    finally:
        asyncio.run(client._http.aclose())

    assert captured.get("buyer_ip") == "203.0.113.9"
    assert result.get("success") is True


async def _noop():
    return None


# ── Buy-now × Address FSM integration ────────────────────────────────────────
# Regression: the address FSM owns the turn for "buy it now"/"go to checkout",
# so handle_buy_now (the code that emits add_to_cart) never runs and nothing
# adds the product to the cart — the widget then binds an EMPTY cart to the
# checkout URL and Shopify bounces /checkout back to the homepage. The FSM must
# resolve the target product (like handle_buy_now) and emit add_to_cart BEFORE
# redirect_checkout_with_address on completion.

def _buy_now_test_harness(meta, monkeypatch, resolver=None):
    from src.app.agent.brain import core as brain_core
    from src.app.agent.classifier import IntentResult

    class FakeClassifier:
        async def classify(self, message, lang):
            return IntentResult(intent="search", confidence=0.9, via="test")

    class FakeSession:
        def __init__(self):
            self.meta = dict(meta)
            self.session = {}

        async def get_meta(self, tenant_id, session_id):
            return dict(self.meta)

        async def save_meta(self, tenant_id, session_id, new_meta):
            self.meta = {**self.meta, **new_meta}
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

    monkeypatch.setattr(brain_core, "append_live_navigation", lambda *a, **k: None)
    monkeypatch.setattr(brain_core, "get_classifier", lambda: FakeClassifier())
    monkeypatch.setattr(brain_core, "get_session_facts_service", lambda *a, **k: FakeFacts())
    monkeypatch.setattr(brain_core, "get_beta_logger", lambda: FakeBeta())
    if resolver is not None:
        monkeypatch.setattr(brain_core, "_resolve_product_for_add", resolver)

    return brain_core, FakeSession()


def test_buy_now_start_persists_resolved_product(monkeypatch):
    """On the START turn the FSM resolves the buy-now product and persists it so
    the completion turn can emit add_to_cart (no checkout redirect yet)."""
    async def fake_resolve(message, lower, session_id, active_recommendations, *,
                           page_context, store_client, tenant_id, session_service):
        assert page_context.get("product_id") == 100
        return {
            "id": 100, "name": "Formal Shoes", "variant_id": 5,
            "permalink": "https://store.com/products/formal-shoes",
        }

    brain_core, fake_session = _buy_now_test_harness(
        {"language": "en", "address_state": "idle"}, monkeypatch, resolver=fake_resolve,
    )
    result = asyncio.run(brain_core.ask_brain(
        session_id="sess_buynow_start",
        user_message="buy it now",
        store_context={"url": "https://speako-demo.com"},
        page_context={"url": "https://speako-demo.com/products/formal-shoes",
                      "product_id": 100, "variant_id": 5},
        language="en",
        store_client=object(),
        session_service=fake_session,
        redis=None,
        db_session_factory=None,
    ))

    types = [a.get("type") for a in (result.get("ui_actions") or [])]
    assert "add_to_cart" not in types
    assert "redirect_checkout_with_address" not in types
    assert fake_session.meta.get("buy_now_product", {}).get("id") == 100
    assert fake_session.meta.get("address_state") == S.COLLECTING_PHONE


def test_buy_now_completion_emits_add_to_cart_before_redirect(monkeypatch):
    """Closing the flow on confirm must emit add_to_cart (from the stashed
    product) BEFORE redirect_checkout_with_address — so the widget binds a cart
    that actually contains the product."""
    async def fake_save(session_id, tenant_id, phone, address_data):
        return None
    monkeypatch.setattr("src.app.modules.users.address_service.save_address", fake_save)

    brain_core, fake_session = _buy_now_test_harness({
        "language": "en",
        "address_state": "confirming",
        "address_data": {
            "first_name": "Asha", "last_name": "Nair",
            "address_line1": "Flat 12, MG Road", "city": "Kochi",
            "state": "California", "postcode": "90001",
            "phone": "9876543210", "email": "asha@example.com",
        },
        "buy_now_product": {
            "id": 100, "variant_id": 5,
            "permalink": "https://store.com/products/formal-shoes",
        },
    }, monkeypatch)

    result = asyncio.run(brain_core.ask_brain(
        session_id="sess_buynow_confirm",
        user_message="yes",
        store_context={"url": "https://speako-demo.com"},
        page_context={"url": "https://speako-demo.com/products/formal-shoes"},
        language="en",
        store_client=object(),
        session_service=fake_session,
        redis=None,
        db_session_factory=None,
    ))

    actions = list(result.get("ui_actions") or [])
    types = [a.get("type") for a in actions]
    assert types and types[0] == "add_to_cart", types
    assert actions[0]["payload"]["product_id"] == 100
    assert actions[0]["payload"]["variant_id"] == 5
    assert actions[0]["payload"]["handle"] == "formal-shoes"  # derived from permalink
    redirect = next((a for a in actions if a.get("type") == "redirect_checkout_with_address"), None)
    assert redirect is not None
    assert redirect["payload"]["shipping"]["city"] == "Kochi"
    # Stash is consumed on completion — a later flow must not re-add it.
    assert fake_session.meta.get("buy_now_product") is None


# ── Multi-order session memory-leak regressions (core.py) ────────────────────
# The address FSM returns COMPLETE on confirm so the SAME turn can emit
# redirect_checkout_with_address (+ the prepended add_to_cart). But the state
# PERSISTED for the next turn must be reset to idle with a cleared address —
# otherwise a second purchase in the same session re-triggers the first order's
# redirect ("sticky COMPLETE" leak). These drive full turns through core.py.

def test_checkout_dispatch_resets_fsm_for_next_order(monkeypatch):
    """After the FSM dispatches redirect_checkout_with_address, the persisted
    session meta must be reset to idle / empty address / no stashed product, so
    the NEXT order in the same session starts from a clean slate. THIS turn's
    redirect is unaffected."""
    async def fake_save(session_id, tenant_id, phone, address_data):
        return None
    monkeypatch.setattr("src.app.modules.users.address_service.save_address", fake_save)

    brain_core, fake_session = _buy_now_test_harness({
        "language": "en",
        "address_state": "confirming",
        "address_data": {
            "first_name": "Asha", "last_name": "Nair",
            "address_line1": "Flat 12, MG Road", "city": "Kochi",
            "state": "California", "postcode": "90001",
            "phone": "9876543210", "email": "asha@example.com",
        },
        "buy_now_product": {
            "id": 100, "variant_id": 5,
            "permalink": "https://store.com/products/formal-shoes",
        },
    }, monkeypatch)

    result = asyncio.run(brain_core.ask_brain(
        session_id="sess_reset_after_dispatch",
        user_message="yes",
        store_context={"url": "https://speako-demo.com"},
        page_context={"url": "https://speako-demo.com/products/formal-shoes"},
        language="en",
        store_client=object(),
        session_service=fake_session,
        redis=None,
        db_session_factory=None,
    ))

    # This turn still fires the redirect for the CURRENT order.
    types = [a.get("type") for a in (result.get("ui_actions") or [])]
    assert "redirect_checkout_with_address" in types
    # …but the state persisted for the NEXT turn is wiped clean.
    assert fake_session.meta.get("address_state") == S.IDLE
    assert fake_session.meta.get("address_data") == {}
    assert fake_session.meta.get("buy_now_product") is None


def test_stale_complete_state_wiped_on_new_checkout(monkeypatch):
    """A session that arrives still in COMPLETE from a PREVIOUS order (legacy
    snapshot / reconnect) must NOT re-emit that order's redirect when the
    customer starts a new checkout. The stale COMPLETE is treated as IDLE with a
    cleared address, so a fresh flow begins (asks for the phone) instead."""
    brain_core, fake_session = _buy_now_test_harness({
        "language": "en",
        "address_state": "complete",
        "address_data": {
            "first_name": "Asha", "last_name": "Nair",
            "address_line1": "Flat 12, MG Road", "city": "Kochi",
            "state": "California", "postcode": "90001",
            "phone": "9876543210", "email": "asha@example.com",
        },
    }, monkeypatch)

    result = asyncio.run(brain_core.ask_brain(
        session_id="sess_stale_complete",
        user_message="go to checkout",
        store_context={"url": "https://speako-demo.com"},
        page_context={"url": "https://speako-demo.com/products/formal-shoes",
                      "page_type": "product"},
        language="en",
        store_client=object(),
        session_service=fake_session,
        redis=None,
        db_session_factory=None,
    ))

    # The OLD order must NOT be re-triggered.
    types = [a.get("type") for a in (result.get("ui_actions") or [])]
    assert "redirect_checkout_with_address" not in types
    # A fresh collection flow begins from the phone.
    assert "phone" in (result.get("response_text") or "").lower()
    assert fake_session.meta.get("address_state") == S.COLLECTING_PHONE
    # The stale address is gone (not carried into the new flow).
    assert fake_session.meta.get("address_data", {}).get("city") != "Kochi"

