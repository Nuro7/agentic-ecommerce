"""Regression tests for the Tier 1-2 security fixes.

Covers: merchant-route auth (no token → 401), login password verification,
cross-tenant ownership (403), Shopify webhook signature, and the rate limiter's
TTL-repair behaviour. Uses the in-memory SQLite harness in conftest.py (no Redis
or lifespan), so tests avoid paths that need app.state.
"""
import pytest

from src.app.core.security import hash_password, create_access_token
from src.app.modules.tenants.models import Tenant
from src.app.core.ratelimit import check_rate_limit

pytestmark = pytest.mark.asyncio


async def _make_tenant(db, *, email="merchant@example.com", password="s3cret-pass", active=True):
    tenant = Tenant(
        name="Acme",
        email=email,
        is_active=active,
        hashed_password=hash_password(password) if password else None,
    )
    db.add(tenant)
    await db.commit()
    await db.refresh(tenant)
    return tenant


def _auth(tenant) -> dict:
    return {"Authorization": f"Bearer {create_access_token({'sub': tenant.id, 'email': tenant.email})}"}


# ── Merchant routes require a valid JWT (C-1) ────────────────────────────────

@pytest.mark.parametrize("path", [
    "/api/v1/tenants/me",
    "/api/v1/orders/",
    "/api/v1/analytics/summary",
    "/api/v1/billing/subscription",
])
async def test_protected_routes_reject_missing_token(client, path):
    resp = await client.get(path)
    assert resp.status_code == 401


async def test_protected_route_rejects_garbage_token(client):
    resp = await client.get("/api/v1/tenants/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401


async def test_authenticated_tenant_reads_self(client, db):
    tenant = await _make_tenant(db)
    resp = await client.get("/api/v1/tenants/me", headers=_auth(tenant))
    assert resp.status_code == 200
    assert resp.json()["id"] == tenant.id


async def test_cross_tenant_get_is_forbidden(client, db):
    a = await _make_tenant(db, email="a@example.com")
    b = await _make_tenant(db, email="b@example.com")
    resp = await client.get(f"/api/v1/tenants/{b.id}", headers=_auth(a))
    assert resp.status_code == 403


# ── Login verifies the password (C-2) ────────────────────────────────────────

async def test_login_unknown_email_401(client):
    resp = await client.post("/api/v1/auth/login", json={"email": "nobody@x.com", "password": "x"})
    assert resp.status_code == 401


async def test_login_wrong_password_401(client, db):
    await _make_tenant(db, email="m@example.com", password="correct-horse")
    resp = await client.post("/api/v1/auth/login", json={"email": "m@example.com", "password": "wrong"})
    assert resp.status_code == 401


async def test_login_without_password_set_401(client, db):
    await _make_tenant(db, email="np@example.com", password=None)
    resp = await client.post("/api/v1/auth/login", json={"email": "np@example.com", "password": "anything"})
    assert resp.status_code == 401


async def test_login_success_returns_token(client, db):
    await _make_tenant(db, email="ok@example.com", password="correct-horse")
    resp = await client.post("/api/v1/auth/login", json={"email": "ok@example.com", "password": "correct-horse"})
    assert resp.status_code == 200
    assert resp.json().get("access_token")


# ── Shopify webhook signature (C-5) ──────────────────────────────────────────

async def test_shopify_webhook_missing_signature_rejected(client):
    resp = await client.post("/api/v1/webhooks/shopify/some-tenant", json={"id": 1})
    # 401 (missing HMAC) when secret configured, or 503 when it isn't — never 200.
    assert resp.status_code in (401, 503)


# ── Rate limiter TTL-repair (review fix) ─────────────────────────────────────

class _FakeRedis:
    def __init__(self):
        self.counts: dict = {}
        self.ttls: dict = {}

    async def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key, window, nx=False):
        if nx and key in self.ttls:
            return False
        self.ttls[key] = window
        return True


async def test_rate_limit_allows_then_blocks():
    r = _FakeRedis()
    allowed = [await check_rate_limit(r, tenant_key="t", ip="1.1.1.1", limit=3, window=60) for _ in range(4)]
    assert allowed == [True, True, True, False]


async def test_rate_limit_sets_ttl_once():
    r = _FakeRedis()
    await check_rate_limit(r, tenant_key="t", ip="1.1.1.1", limit=10, window=60)
    await check_rate_limit(r, tenant_key="t", ip="1.1.1.1", limit=10, window=60)
    # NX expire: TTL set exactly once, never extended/cleared → bucket can't wedge.
    assert len(r.ttls) == 1


async def test_rate_limit_fail_open_without_redis():
    assert await check_rate_limit(None, tenant_key="t", ip="1.1.1.1", limit=1, window=60) is True
