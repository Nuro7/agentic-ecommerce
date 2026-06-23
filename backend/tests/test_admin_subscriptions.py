"""Admin API: assign a plan to a chosen tenant (activates the subscription, no payment).

Guarded by X-Admin-Token / ADMIN_API_KEY. Once assigned, the billing 402 gate passes.
The in-memory DB is shared across the session, so each seed uses unique names/emails.
"""
import uuid

import pytest

from src.app.config import settings
from src.app.modules.billing.models import Plan
from src.app.modules.billing.dependencies import enforce_conversation_quota
from src.app.modules.tenants.models import Tenant

pytestmark = pytest.mark.asyncio

ADMIN = {"X-Admin-Token": "testkey"}


async def _seed(db, *, price=0, credits=200):
    uid = uuid.uuid4().hex[:8]
    plan = Plan(name=f"starter-{uid}", price_monthly=price, max_conversations=credits, max_stores=1)
    tenant = Tenant(name=f"Acme-{uid}", email=f"acme-{uid}@example.com")
    db.add(plan)
    db.add(tenant)
    await db.commit()
    await db.refresh(plan)
    await db.refresh(tenant)
    return plan, tenant


async def test_assign_plan_activates_and_quota_passes(client, db, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "testkey")
    plan, tenant = await _seed(db)

    r = await client.post(
        "/api/v1/admin/subscriptions",
        headers=ADMIN,
        json={"tenant_id": tenant.id, "plan": plan.name},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "active"
    assert body["plan_id"] == plan.id
    assert body["tenant_id"] == tenant.id

    # The 402 gate now passes for this tenant (no exception).
    await enforce_conversation_quota(tenant.id, db)


async def test_assign_plan_by_plan_id_works(client, db, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "testkey")
    plan, tenant = await _seed(db)
    r = await client.post(
        "/api/v1/admin/subscriptions",
        headers=ADMIN,
        json={"tenant_id": tenant.id, "plan": plan.id},  # resolve by id, not name
    )
    assert r.status_code == 200, r.text
    assert r.json()["plan_id"] == plan.id


async def test_missing_token_rejected(client, db, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "testkey")
    plan, tenant = await _seed(db)
    r = await client.post(
        "/api/v1/admin/subscriptions",
        json={"tenant_id": tenant.id, "plan": plan.name},
    )
    assert r.status_code == 401


async def test_wrong_token_rejected(client, db, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "testkey")
    plan, tenant = await _seed(db)
    r = await client.post(
        "/api/v1/admin/subscriptions",
        headers={"X-Admin-Token": "wrong"},
        json={"tenant_id": tenant.id, "plan": plan.name},
    )
    assert r.status_code == 401


async def test_admin_disabled_without_key(client, db, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "")  # disabled
    plan, tenant = await _seed(db)
    r = await client.post(
        "/api/v1/admin/subscriptions",
        headers=ADMIN,
        json={"tenant_id": tenant.id, "plan": plan.name},
    )
    assert r.status_code == 503


async def test_unknown_tenant_404(client, db, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "testkey")
    plan, _ = await _seed(db)
    r = await client.post(
        "/api/v1/admin/subscriptions",
        headers=ADMIN,
        json={"tenant_id": "does-not-exist", "plan": plan.name},
    )
    assert r.status_code == 404


async def test_unknown_plan_404(client, db, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "testkey")
    _, tenant = await _seed(db)
    r = await client.post(
        "/api/v1/admin/subscriptions",
        headers=ADMIN,
        json={"tenant_id": tenant.id, "plan": "nonexistent-plan"},
    )
    assert r.status_code == 404
