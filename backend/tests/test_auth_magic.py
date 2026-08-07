"""Tests for the merchant dashboard auth flow (magic link + set-password).

Covers the two merchant states: those with a password (normal login) and
Shopify-OAuth-installed merchants with no password (adaptive status + magic
link + one-time password set).
"""
import pytest

from src.app.core.security import hash_password
from src.app.modules.tenants.models import Tenant

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _magic_link_base(monkeypatch):
    """Point magic links at a base URL so the dev-link fallback is returned
    (SMTP is not configured in the test harness)."""
    from src.app.config import settings as app_settings
    monkeypatch.setattr(app_settings, "backend_url", "http://dashboard-test")
    monkeypatch.setattr(app_settings, "dashboard_url", "http://dashboard-test")


async def _make_tenant(db, *, email, password=None):
    tenant = Tenant(
        name="Acme",
        email=email,
        is_active=True,
        hashed_password=hash_password(password) if password else None,
    )
    db.add(tenant)
    await db.commit()
    await db.refresh(tenant)
    return tenant


def _extract_token(dev_link: str) -> str:
    return dev_link.split("token=", 1)[1]


# ── /auth/status (adaptive login screen) ─────────────────────────────────────

async def test_status_unknown_email_not_recognized(client):
    resp = await client.post("/api/v1/auth/status", json={"email": "nobody@x.com"})
    assert resp.status_code == 200
    assert resp.json() == {"recognized": False, "has_password": False}


async def test_status_oauth_merchant_recognized_without_password(client, db):
    await _make_tenant(db, email="cartify@example.com", password=None)
    resp = await client.post("/api/v1/auth/status", json={"email": "cartify@example.com"})
    assert resp.json() == {"recognized": True, "has_password": False}


async def test_status_merchant_with_password(client, db):
    await _make_tenant(db, email="pw@example.com", password="correct-horse")
    resp = await client.post("/api/v1/auth/status", json={"email": "pw@example.com"})
    assert resp.json() == {"recognized": True, "has_password": True}


# ── Magic link flow (no SMTP configured → dev_link returned) ─────────────────

async def test_magic_request_unknown_email_no_link(client):
    resp = await client.post("/api/v1/auth/magic-request", json={"email": "nobody@x.com"})
    assert resp.status_code == 200
    assert resp.json() == {"sent": False, "dev_link": None}


async def test_magic_request_returns_dev_link_when_smtp_unset(client, db):
    await _make_tenant(db, email="reqlink@example.com", password=None)
    resp = await client.post("/api/v1/auth/magic-request", json={"email": "reqlink@example.com"})
    body = resp.json()
    assert body["sent"] is False          # SMTP not configured in CI
    assert body["dev_link"] and "token=" in body["dev_link"]


async def test_magic_verify_issues_tokens_and_flags_password_needed(client, db):
    await _make_tenant(db, email="mverify@example.com", password=None)
    link = (await client.post(
        "/api/v1/auth/magic-request", json={"email": "mverify@example.com"}
    )).json()["dev_link"]

    resp = await client.post("/api/v1/auth/magic-verify", json={"token": _extract_token(link)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"] and body["refresh_token"]
    assert body["needs_password"] is True


async def test_magic_token_is_single_use(client, db):
    await _make_tenant(db, email="one@example.com", password=None)
    link = (await client.post(
        "/api/v1/auth/magic-request", json={"email": "one@example.com"}
    )).json()["dev_link"]

    first = await client.post("/api/v1/auth/magic-verify", json={"token": _extract_token(link)})
    assert first.status_code == 200

    second = await client.post("/api/v1/auth/magic-verify", json={"token": _extract_token(link)})
    assert second.status_code == 401


# ── Set password → then normal login works ───────────────────────────────────

async def test_full_first_login_flow(client, db):
    tenant = await _make_tenant(db, email="firstlogin@example.com", password=None)
    link = (await client.post(
        "/api/v1/auth/magic-request", json={"email": "firstlogin@example.com"}
    )).json()["dev_link"]
    verified = (await client.post(
        "/api/v1/auth/magic-verify", json={"token": _extract_token(link)}
    )).json()

    headers = {"Authorization": f"Bearer {verified['access_token']}"}
    set_resp = await client.post(
        "/api/v1/auth/set-password",
        json={"password": "new-secret-123", "confirm_password": "new-secret-123"},
        headers=headers,
    )
    assert set_resp.status_code == 204

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "firstlogin@example.com", "password": "new-secret-123"},
    )
    assert login.status_code == 200
    assert login.json().get("access_token")


async def test_set_password_requires_auth(client, db):
    await _make_tenant(db, email="anon@example.com", password=None)
    resp = await client.post(
        "/api/v1/auth/set-password",
        json={"password": "x-12345678", "confirm_password": "x-12345678"},
    )
    assert resp.status_code == 401


async def test_set_password_mismatch_rejected(client, db):
    tenant = await _make_tenant(db, email="mismatch@example.com", password="correct-horse")
    from src.app.core.security import create_access_token
    headers = {"Authorization": f"Bearer {create_access_token({'sub': tenant.id, 'email': tenant.email})}"}
    resp = await client.post(
        "/api/v1/auth/set-password",
        json={"password": "aaa111", "confirm_password": "bbb222"},
        headers=headers,
    )
    assert resp.status_code == 422


# ── Email-only login (MVP) ───────────────────────────────────────────────────

async def test_email_login_existing_tenant_issues_token(client, db):
    tenant = await _make_tenant(db, email="mailonly@example.com", password=None)
    resp = await client.post("/api/v1/auth/email-login", json={"email": "mailonly@example.com"})
    assert resp.status_code == 200
    assert resp.json().get("access_token")


async def test_email_login_unknown_email_401(client):
    resp = await client.post("/api/v1/auth/email-login", json={"email": "ghost@example.com"})
    assert resp.status_code == 401


async def test_email_login_token_scopes_to_that_tenant(client, db):
    tenant = await _make_tenant(db, email="scoped@example.com", password=None)
    tokens = (await client.post(
        "/api/v1/auth/email-login", json={"email": "scoped@example.com"}
    )).json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    me = await client.get("/api/v1/tenants/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["id"] == tenant.id


# ── Refresh token rotation ───────────────────────────────────────────────────

async def test_refresh_rotates_and_old_token_retired(client, db):
    tenant = await _make_tenant(db, email="rot@example.com", password="correct-horse")
    login = (await client.post(
        "/api/v1/auth/login",
        json={"email": "rot@example.com", "password": "correct-horse"},
    )).json()
    old_refresh = login["refresh_token"]

    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert resp.status_code == 200
    new_refresh = resp.json()["refresh_token"]
    assert new_refresh and new_refresh != old_refresh

    reused = await client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert reused.status_code == 401
