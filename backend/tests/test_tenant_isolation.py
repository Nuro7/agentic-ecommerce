"""Multi-tenant isolation regression tests.

P0-1 — reject requests/WS with no resolvable tenant in production. When enforcement
is on, an unresolved tenant must NOT fall back to the shared global store client
(which would merge two tenants' catalogs/sessions). When enforcement is off (dev),
the global fallback still works.
"""
import pytest
from fastapi import HTTPException

from src.app.config import settings
from src.app.modules.tenants.dependencies import (
    get_tenant_store_client,
    resolve_tenant_store_client_for_ws,
)

pytestmark = pytest.mark.asyncio


class _FakeState:
    """Stand-in for request.app.state / WS app_state."""
    def __init__(self, *, store_client=None, redis=None):
        if store_client is not None:
            self.store_client = store_client
        if redis is not None:
            self.redis = redis


class _FakeApp:
    def __init__(self, state):
        self.state = state


class _FakeQS:
    def __init__(self, data=None):
        self._d = data or {}

    def get(self, key, default=""):
        return self._d.get(key, default)


class _FakeRequest:
    """Minimal Request: only the attrs get_tenant_store_client touches."""
    def __init__(self, *, state, query=None, headers=None):
        self.app = _FakeApp(state)
        self.query_params = _FakeQS(query)
        self.headers = _FakeQS(headers)


# A dummy db — TenantRepository(db) is constructed but its methods are only called
# when a shop/X-Tenant-ID is present, which these no-tenant tests never supply.
_DUMMY_DB = object()


# ── HTTP path: get_tenant_store_client ───────────────────────────────────────

async def test_http_rejects_unresolved_when_enforced(monkeypatch):
    monkeypatch.setattr(settings, "enforce_tenant_resolution", True)
    # No shop, no X-Tenant-ID, and no global store_client either.
    req = _FakeRequest(state=_FakeState())
    with pytest.raises(HTTPException) as exc:
        await get_tenant_store_client(req, _DUMMY_DB)
    assert exc.value.status_code == 400
    assert exc.value.detail == "Unresolved tenant"


async def test_http_rejects_unresolved_even_with_global_client(monkeypatch):
    # Enforcement must win over the presence of a global client — otherwise prod
    # would silently serve every no-tenant caller from the same shared store.
    monkeypatch.setattr(settings, "enforce_tenant_resolution", True)
    req = _FakeRequest(state=_FakeState(store_client="GLOBAL"))
    with pytest.raises(HTTPException) as exc:
        await get_tenant_store_client(req, _DUMMY_DB)
    assert exc.value.status_code == 400


async def test_http_falls_back_when_not_enforced(monkeypatch):
    monkeypatch.setattr(settings, "enforce_tenant_resolution", False)
    req = _FakeRequest(state=_FakeState(store_client="GLOBAL"))
    result = await get_tenant_store_client(req, _DUMMY_DB)
    assert result == "GLOBAL"


# ── WS path: resolve_tenant_store_client_for_ws ──────────────────────────────

async def test_ws_resolver_returns_none_when_unresolved():
    # Pure resolver: no shop/tenant_id and no global client → (None, None). The WS
    # handler turns this into a 4003 close when settings.require_tenant is true.
    client, tid = await resolve_tenant_store_client_for_ws(
        shop="", tenant_id="", app_state=_FakeState(), db=_DUMMY_DB,
    )
    assert client is None
    assert tid is None


async def test_ws_resolver_falls_back_to_global_in_dev():
    # In dev the resolver still returns the global client (tenant_id None); the
    # handler only rejects when enforcement is on.
    client, tid = await resolve_tenant_store_client_for_ws(
        shop="", tenant_id="", app_state=_FakeState(store_client="GLOBAL"), db=_DUMMY_DB,
    )
    assert client == "GLOBAL"
    assert tid is None


# ── require_tenant property semantics ────────────────────────────────────────

async def test_require_tenant_defaults_to_production(monkeypatch):
    monkeypatch.setattr(settings, "enforce_tenant_resolution", None)
    monkeypatch.setattr(settings, "environment", "production")
    assert settings.require_tenant is True
    monkeypatch.setattr(settings, "environment", "dev")
    assert settings.require_tenant is False


async def test_require_tenant_explicit_flag_overrides_environment(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "enforce_tenant_resolution", False)
    assert settings.require_tenant is False


# ── P0-2: session keys are tenant-namespaced ─────────────────────────────────

async def test_session_meta_is_unreadable_across_tenants():
    from src.app.agent.memory.session import SessionService
    svc = SessionService(redis_client=None)  # in-memory store
    await svc.save_meta("watch-tenant", "sess-shared-id",
                        {"last_products": [{"id": 1, "name": "Rolex"}]})
    # Same session_id, different tenant → must NOT see watch-tenant's products.
    assert (await svc.get_meta("watch-tenant", "sess-shared-id"))["last_products"]
    assert await svc.get_meta("kitchen-tenant", "sess-shared-id") == {}


async def test_session_state_is_unreadable_across_tenants():
    from src.app.agent.memory.session import SessionService
    svc = SessionService(redis_client=None)
    await svc.update_session("watch-tenant", "sess-shared-id",
                             last_products=[{"id": 9, "name": "G-Shock"}])
    assert (await svc.get_session("watch-tenant", "sess-shared-id"))["last_products"]
    # Kitchen tenant gets a clean default state, never the watch products.
    assert (await svc.get_session("kitchen-tenant", "sess-shared-id"))["last_products"] == []


async def test_session_cart_is_unreadable_across_tenants():
    from src.app.agent.memory.session import SessionService
    svc = SessionService(redis_client=None)
    await svc.save_cart("watch-tenant", "sess-shared-id", {"is_empty": False, "item_count": 2})
    assert (await svc.get_cart("watch-tenant", "sess-shared-id"))["item_count"] == 2
    # Kitchen tenant must never see the watch tenant's cart (no items leaked).
    assert (await svc.get_cart("kitchen-tenant", "sess-shared-id")).get("item_count", 0) in (0, None)


# ── P0-2: WS token is bound to the tenant ────────────────────────────────────

async def test_ws_token_valid_for_minting_tenant(monkeypatch):
    monkeypatch.setenv("SHARED_SECRET", "unit-test-secret")
    monkeypatch.delenv("MVP_MODE", raising=False)
    from src.app.agent.gemini_client import generate_ws_token, validate_ws_token
    tok = generate_ws_token("tenant-A", "sess-12345678")
    assert validate_ws_token(tok, "tenant-A", "sess-12345678") is True


async def test_ws_token_rejected_for_other_tenant(monkeypatch):
    monkeypatch.setenv("SHARED_SECRET", "unit-test-secret")
    monkeypatch.delenv("MVP_MODE", raising=False)
    from src.app.agent.gemini_client import generate_ws_token, validate_ws_token
    tok = generate_ws_token("tenant-A", "sess-12345678")
    # A token minted for tenant A must NOT validate when presented for tenant B.
    assert validate_ws_token(tok, "tenant-B", "sess-12345678") is False


# ── P0-3: facts store is tenant-namespaced ───────────────────────────────────

async def test_facts_unreadable_across_tenants():
    from src.app.agent.memory.facts import SessionFactsService
    svc = SessionFactsService(redis_client=None)  # in-memory _mem
    await svc.update(
        "watch-tenant", "sess-shared-id", "I like the Rolex Submariner",
        [{"result": {"id": 1, "name": "Rolex Submariner"}}],
    )
    # Watch tenant remembers the last product...
    assert (await svc.get("watch-tenant", "sess-shared-id")).get("last_product_name") == "Rolex Submariner"
    # ...but the kitchen tenant (same session_id) must never see it.
    assert await svc.get("kitchen-tenant", "sess-shared-id") == {}


# ── P0-5: catalog cache key is tenant-scoped ─────────────────────────────────

class _FakeStoreClient:
    base_url = "https://shared.example"  # identical for both tenants

    async def get_categories(self):
        return [{"name": "Watches", "count": 3}]

    async def search_products(self, **kwargs):
        return []


async def test_catalog_cache_is_tenant_scoped():
    from src.app.agent.brain import core
    core._catalog_cache.clear()
    client = _FakeStoreClient()  # SAME client object for both tenants
    a = await core._get_store_catalog("tenant-A", client)
    b = await core._get_store_catalog("tenant-B", client)
    assert a == b  # same store → same content
    # ...but cached under two distinct, tenant-prefixed keys (no cross-tenant share).
    keys = set(core._catalog_cache.keys())
    assert any(k.startswith("tenant-A|") for k in keys)
    assert any(k.startswith("tenant-B|") for k in keys)
    assert len(keys) == 2


# ── P0-6: MVP_MODE cannot bypass auth in production ──────────────────────────

async def test_mvp_mode_bypass_disabled_in_production(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setenv("SHARED_SECRET", "unit-test-secret")
    monkeypatch.setenv("MVP_MODE", "true")
    from src.app.agent.gemini_client import validate_ws_token
    # Even with MVP_MODE=true, prod requires a valid token.
    assert validate_ws_token("", "tenant-A", "sess-12345678") is False
    assert validate_ws_token("garbage.token", "tenant-A", "sess-12345678") is False


async def test_mvp_mode_bypass_active_in_dev(monkeypatch):
    monkeypatch.setattr(settings, "environment", "dev")
    monkeypatch.setenv("SHARED_SECRET", "unit-test-secret")
    monkeypatch.setenv("MVP_MODE", "true")
    from src.app.agent.gemini_client import validate_ws_token
    # Dev convenience preserved: MVP_MODE bypass still works off-prod.
    assert validate_ws_token("", "tenant-A", "sess-12345678") is True


async def test_missing_secret_rejected_in_production(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.delenv("SHARED_SECRET", raising=False)
    monkeypatch.delenv("MVP_MODE", raising=False)
    from src.app.agent.gemini_client import validate_ws_token
    # No secret in prod → reject, never silently disable auth.
    assert validate_ws_token("", "tenant-A", "sess-12345678") is False
