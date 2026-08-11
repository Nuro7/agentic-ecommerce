"""API tests for /api/v1/overlay/* — the server-side Storefront proxy.

The real StorefrontService is swapped for a fake via FastAPI dependency
overrides so no network / DB is touched; the point is to verify route
contracts, payload mapping, and error translation.
"""
import pytest
from httpx import ASGITransport, AsyncClient

from src.app.api.v1.overlay import resolve_storefront
from src.app.integrations.shopify.storefront import StorefrontServiceError
from src.app.server import create_app


def _cart():
    return {
        "cart_id": "gid://shopify/Cart/c1",
        "checkout_url": "https://demo.myshopify.com/checkout/c/abc",
        "total_quantity": 2,
        "subtotal": {"amount": 99.98, "currencyCode": "INR"},
        "total": {"amount": 99.98, "currencyCode": "INR"},
        "discount_codes": [{"code": "SALE10", "applicable": True}],
        "lines": [{
            "id": "gid://shopify/CartLine/1",
            "quantity": 2,
            "variant_id": "gid://shopify/ProductVariant/11",
            "product_handle": "runner-shoe",
            "product_title": "Runner Shoe",
            "image": "https://cdn/shoe.jpg",
            "unit_price": {"amount": 49.99, "currencyCode": "INR"},
            "line_total": {"amount": 99.98, "currencyCode": "INR"},
        }],
    }


class FakeStorefront:
    """Duck-typed stand-in for StorefrontService used by the overlay router."""

    def __init__(self, configured=True, fail_with=None, product=None):
        self._configured = configured
        self._fail = fail_with
        self._product = product
        self.calls = []

    @property
    def is_configured(self):
        return self._configured

    def _maybe_fail(self):
        if self._fail:
            raise self._fail

    async def search_products(self, query, first=20, **kw):
        self._maybe_fail()
        self.calls.append(("search", query, first))
        return [{"handle": "runner-shoe", "title": "Runner Shoe", "price": {"amount": 49.99, "currencyCode": "INR"}}]

    async def get_product(self, handle, **kw):
        self._maybe_fail()
        self.calls.append(("product", handle))
        return self._product

    async def cart_create(self, *, lines=None, **kw):
        self._maybe_fail()
        self.calls.append(("cart_create", lines))
        return _cart()

    async def cart_get(self, cart_id, **kw):
        self._maybe_fail()
        self.calls.append(("cart_get", cart_id))
        return _cart()

    async def cart_lines_add(self, cart_id, lines, **kw):
        self._maybe_fail()
        self.calls.append(("lines_add", cart_id, lines))
        return _cart()

    async def cart_discount_codes_update(self, cart_id, codes, **kw):
        self._maybe_fail()
        self.calls.append(("discount", cart_id, codes))
        return _cart()

    async def cart_buyer_identity_update(self, cart_id, buyer_identity, **kw):
        self._maybe_fail()
        self.calls.append(("buyer", cart_id, buyer_identity))
        return _cart()

    async def cart_delivery_addresses_replace(self, cart_id, addresses, **kw):
        self._maybe_fail()
        self.calls.append(("address", cart_id, addresses))
        return _cart()


@pytest.fixture
async def client():
    app = create_app()
    app.dependency_overrides[resolve_storefront] = lambda: FakeStorefront(product={
        "handle": "runner-shoe", "title": "Runner Shoe",
        "price": {"amount": 49.99, "currencyCode": "INR"},
    })
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_search_returns_products(client):
    resp = await client.get("/api/v1/overlay/search", params={"q": "sneakers", "shop": "demo.myshopify.com"})
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["handle"] == "runner-shoe"
    assert data[0]["price"]["amount"] == 49.99


@pytest.mark.asyncio
async def test_product_found_and_not_found(client):
    ok = await client.get("/api/v1/overlay/product/runner-shoe")
    assert ok.status_code == 200
    assert ok.json()["title"] == "Runner Shoe"

    app = create_app()
    app.dependency_overrides[resolve_storefront] = lambda: FakeStorefront(product=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        missing = await c.get("/api/v1/overlay/product/missing")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_cart_create_maps_merchandise_id(client):
    resp = await client.post("/api/v1/overlay/cart", json={
        "shop": "demo.myshopify.com",
        "lines": [{"merchandise_id": "gid://shopify/ProductVariant/11", "quantity": 2}],
    })
    assert resp.status_code == 200
    assert resp.json()["cart_id"] == "gid://shopify/Cart/c1"
    assert resp.json()["checkout_url"].endswith("/checkout/c/abc")
    assert resp.json()["lines"][0]["quantity"] == 2


@pytest.mark.asyncio
async def test_cart_lines_add_passes_through(client):
    resp = await client.post("/api/v1/overlay/cart/lines", json={
        "cart_id": "gid://shopify/Cart/c1",
        "lines": [{"merchandise_id": "gid://shopify/ProductVariant/12", "quantity": 1}],
    })
    assert resp.status_code == 200
    assert resp.json()["discount_codes"] == [{"code": "SALE10", "applicable": True}]


@pytest.mark.asyncio
async def test_cart_discount(client):
    resp = await client.post("/api/v1/overlay/cart/discount", json={
        "cart_id": "gid://shopify/Cart/c1",
        "codes": ["SALE10"],
    })
    assert resp.status_code == 200
    assert resp.json()["discount_codes"][0]["code"] == "SALE10"


@pytest.mark.asyncio
async def test_cart_buyer_binds_address_and_identity(client):
    resp = await client.post("/api/v1/overlay/cart/buyer", json={
        "cart_id": "gid://shopify/Cart/c1",
        "email": "riya@example.com",
        "delivery_address": {
            "address1": "1 Main St",
            "city": "Bengaluru",
            "country_code": "IN",
            "zip": "560001",
        },
    })
    assert resp.status_code == 200
    assert resp.json()["total_quantity"] == 2


@pytest.mark.asyncio
async def test_cart_checkout_returns_checkout_url(client):
    resp = await client.post("/api/v1/overlay/cart/checkout", json={
        "cart_id": "gid://shopify/Cart/c1",
        "email": "riya@example.com",
        "discount_codes": ["SALE10"],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["checkout_url"] == "https://demo.myshopify.com/checkout/c/abc"
    assert body["cart"]["total_quantity"] == 2


@pytest.mark.asyncio
async def test_cart_status(client):
    resp = await client.get("/api/v1/overlay/cart/status", params={"cart_id": "gid://shopify/Cart/c1"})
    assert resp.status_code == 200
    assert resp.json()["checkout_url"] == "https://demo.myshopify.com/checkout/c/abc"


@pytest.mark.asyncio
async def test_storefront_error_maps_to_502():
    app = create_app()
    app.dependency_overrides[resolve_storefront] = lambda: FakeStorefront(
        fail_with=StorefrontServiceError("Storefront error: [{'message': 'throttled'}]")
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/v1/overlay/search", params={"q": "x"})
    assert resp.status_code == 502
    assert "throttled" in resp.json()["errors"][0]["message"]


@pytest.mark.asyncio
async def test_not_configured_returns_400():
    app = create_app()
    app.dependency_overrides[resolve_storefront] = lambda: FakeStorefront(configured=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/v1/overlay/search", params={"q": "x"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_discount_user_errors_map_to_422():
    class ErrorCartStore(FakeStorefront):
        async def cart_buyer_identity_update(self, cart_id, buyer_identity, **kw):
            return {"cart": None, "errors": [{"field": ["cart"], "message": "cart expired", "code": "INVALID"}]}

    app = create_app()
    app.dependency_overrides[resolve_storefront] = lambda: ErrorCartStore()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/v1/overlay/cart/checkout", json={"cart_id": "gid://shopify/Cart/x"})
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "INVALID"


@pytest.mark.asyncio
async def test_checkout_missing_url_is_502():
    class NoUrlStore(FakeStorefront):
        async def cart_buyer_identity_update(self, cart_id, buyer_identity, **kw):
            return {"cart_id": "gid://shopify/Cart/c1", "checkout_url": "", "total_quantity": 0, "discount_codes": []}

    app = create_app()
    app.dependency_overrides[resolve_storefront] = lambda: NoUrlStore()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/v1/overlay/cart/checkout", json={"cart_id": "gid://shopify/Cart/c1"})
    assert resp.status_code == 502