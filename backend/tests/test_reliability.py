"""P2/P3 reliability & correctness fixes.

- P3-15: hot-path errors are logged, not silently swallowed (session clear_session/get_cart).
- P3-14: /ops surfaces DB connectivity + sync-staleness + embeddings health (degrades on SQLite).
- P3-18a: compare_products fetches concurrently and survives one item failing.
- P2-13: the per-tenant store-client cache is size-bounded (oldest-first eviction).
- in_stock fix (migration 0014): webhook/ingest accepts stock-less products.
"""
import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

from src.app.agent.brain.tool_dispatch import execute_tool_call
from src.app.agent.memory.session import SessionService
from src.app.integrations import factory
from src.app.modules.tenants.models import Tenant
from src.app.modules.webhooks.service import WebhookService

pytestmark = pytest.mark.asyncio


# ── P3-15: silent excepts now log (fail-open preserved) ───────────────────────

class _RaisingRedis:
    """Stand-in redis whose every op raises — simulates Redis down / corrupt data."""

    async def get(self, *a, **k):
        raise RuntimeError("redis boom")

    async def delete(self, *a, **k):
        raise RuntimeError("redis boom")


async def test_get_cart_logs_and_returns_empty(caplog):
    svc = SessionService(redis_client=_RaisingRedis())
    with caplog.at_level(logging.WARNING):
        cart = await svc.get_cart("tenant-a", "sess-1")
    assert cart.get("is_empty") is True
    assert cart.get("items") == []
    assert any("Cart cache read failed" in r.message for r in caplog.records)


async def test_clear_session_logs_and_does_not_raise(caplog):
    svc = SessionService(redis_client=_RaisingRedis())
    with caplog.at_level(logging.WARNING):
        await svc.clear_session("tenant-a", "sess-1")  # must not raise
    assert any("Session clear failed" in r.message for r in caplog.records)


# ── P3-14: /ops core signals (PG-only aggregates degrade on SQLite) ───────────

async def test_ops_returns_core_signals(client):
    resp = await client.get("/api/v1/ops")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] is True
    assert "embeddings_provider" in body
    assert "sync_metrics_error" in body or "sync_newest_age_s" in body


# ── P3-18a: compare_products concurrency + resilience ─────────────────────────

_DELAY = 0.10


class _SlowStore:
    """Each call sleeps _DELAY — sequential would be N×_DELAY, concurrent ~1×_DELAY."""

    def __init__(self, fail_name=None):
        self.fail_name = fail_name

    async def search_products(self, query, in_stock_only=False, limit=1):
        await asyncio.sleep(_DELAY)
        if query == self.fail_name:
            raise RuntimeError("store boom")
        return [{"id": abs(hash(query)) % 100000, "name": query, "price": "10", "variations": True}]

    async def get_product_details(self, pid):  # not hit (variations=True short-circuits)
        await asyncio.sleep(_DELAY)
        return {"id": pid, "name": str(pid)}


async def _compare(store, **names):
    result, actions, ids, _ = await execute_tool_call(
        "compare_products", names, "sess-1", None,
        tenant_id="t1", store_client=store, session_service=None,
    )
    return result


async def test_compare_runs_concurrently():
    store = _SlowStore()
    t0 = time.monotonic()
    result = await _compare(store, product_a="Aria Saree", product_b="Mysore Silk")
    elapsed = time.monotonic() - t0
    assert result["count"] == 2
    assert elapsed < 0.18, f"compare not concurrent: {elapsed:.3f}s"


async def test_compare_survives_one_failure():
    store = _SlowStore(fail_name="Broken Product")
    result = await _compare(store, product_a="Good Product", product_b="Broken Product")
    assert result["count"] == 1


# ── P2-13: store-client cache is size-bounded ─────────────────────────────────

def _tenant(i: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"tenant-{i}", platform="custom_api",  # CustomApiClient is network-free
        custom_api_base_url="https://example.test", custom_api_key="",
    )


async def test_client_cache_is_size_bounded(monkeypatch):
    monkeypatch.setattr(factory, "CLIENT_CACHE_MAX", 3)
    factory._CLIENT_CACHE.clear()
    for i in range(5):
        await factory.create_store_client_for_tenant(_tenant(i))
    assert len(factory._CLIENT_CACHE) <= 3
    assert "tenant-0" not in factory._CLIENT_CACHE
    assert "tenant-1" not in factory._CLIENT_CACHE
    assert "tenant-4" in factory._CLIENT_CACHE
    factory._CLIENT_CACHE.clear()


# ── in_stock fix (migration 0014): stock-less webhook upsert ──────────────────

async def test_upsert_stockless_product_succeeds(db):
    t = Tenant(name="Acme", email="acme@example.com")
    db.add(t)
    await db.commit()
    await db.refresh(t)
    # No stock field → in_stock resolves to None (unknown); must ingest, not 500.
    out = await WebhookService(db).upsert_product(
        t.id, {"id": "sku-1", "name": "Mysore Silk Saree", "price": "499"}
    )
    assert out.get("ok") is True


async def test_upsert_with_stock_succeeds(db):
    t = Tenant(name="Acme2", email="acme2@example.com")
    db.add(t)
    await db.commit()
    await db.refresh(t)
    out = await WebhookService(db).upsert_product(
        t.id, {"id": "sku-2", "name": "Malabar Garam Masala", "price": "249", "in_stock": True}
    )
    assert out.get("ok") is True
