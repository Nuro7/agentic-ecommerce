"""
Shopify OAuth App — Install, Callback, Widget Loader, Script Tags.

Install flow:
  1. Merchant clicks Install in Shopify Partners or App Store
  2. GET /api/v1/shopify/install?shop=store.myshopify.com
     → redirects to Shopify OAuth consent screen
  3. Merchant approves → Shopify sends to:
     GET /api/v1/shopify/callback?code=xxx&shop=xxx&hmac=xxx&state=xxx
     → exchanges code for access token
     → creates/updates tenant in DB
     → registers widget script tag on the store
     → redirects merchant to success page

Widget endpoints:
  GET  /api/v1/shopify/widget-loader.js  — dynamic JS loader (registered as script tag)
  POST /api/v1/shopify/setup             — manual script tag registration (dev/testing)
  GET  /api/v1/shopify/script-tags       — list registered script tags
  DELETE /api/v1/shopify/script-tags/{id} — remove a script tag
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy import select

from ....config import settings as _settings

logger = logging.getLogger(__name__)
router = APIRouter()

SHOPIFY_SCOPES = "read_products,write_script_tags,read_script_tags,read_orders,read_customers"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _verify_shopify_hmac(params: dict, secret: str) -> bool:
    """Verify HMAC signature from Shopify OAuth callback."""
    hmac_value = params.pop("hmac", "")
    sorted_params = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    digest = hmac.new(secret.encode(), sorted_params.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, hmac_value)


def _shopify_admin_headers(token: str) -> dict:
    return {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}


def _get_backend_url(request: Request, override: Optional[str] = None) -> str:
    if override:
        return override.rstrip("/")
    env_url = os.getenv("BACKEND_URL", "").strip().rstrip("/")
    if env_url:
        return env_url
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", "localhost"))
    return f"{proto}://{host}"


async def _register_script_tag(shop_domain: str, access_token: str, loader_url: str) -> dict:
    """Register widget-loader.js as a Shopify Script Tag. Removes old tags first."""
    api_version = _settings.shopify_api_version
    tags_url = f"https://{shop_domain}/admin/api/{api_version}/script_tags.json"
    headers = _shopify_admin_headers(access_token)

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Remove old wooagent tags
        list_resp = await client.get(tags_url, headers=headers)
        if list_resp.status_code == 200:
            for tag in list_resp.json().get("script_tags", []):
                src = str(tag.get("src", "")).lower()
                if "wooagent" in src or "widget-loader" in src or "aria" in src:
                    del_url = f"https://{shop_domain}/admin/api/{api_version}/script_tags/{tag['id']}.json"
                    await client.delete(del_url, headers=headers)
                    logger.info("Removed old script tag %s", tag["id"])

        # Register new tag
        resp = await client.post(
            tags_url,
            headers=headers,
            json={"script_tag": {"event": "onload", "src": loader_url, "display_scope": "online_store"}},
        )

    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Shopify rejected script tag: {resp.status_code} {resp.text[:300]}")

    return resp.json().get("script_tag", {})


# ── OAuth Install ─────────────────────────────────────────────────────────────

@router.get("/shopify/install")
async def shopify_install(shop: str, request: Request):
    """
    Step 1 — Initiate OAuth.
    Set this as your App URL in Shopify Partners:
      https://your-backend/api/v1/shopify/install
    """
    if not _settings.shopify_api_key or not _settings.shopify_api_secret:
        return JSONResponse(
            status_code=400,
            content={"error": "SHOPIFY_API_KEY and SHOPIFY_API_SECRET must be set in .env"},
        )

    if not shop.endswith(".myshopify.com"):
        return JSONResponse(status_code=400, content={"error": "Invalid shop domain"})

    # Generate state nonce and store in Redis (TTL 10 min)
    state = secrets.token_urlsafe(32)
    redis = getattr(request.app.state, "redis", None)
    if redis:
        await redis.setex(f"shopify:oauth:state:{state}", 600, shop)

    backend_url = _get_backend_url(request)
    redirect_uri = f"{backend_url}/api/v1/shopify/callback"

    params = {
        "client_id": _settings.shopify_api_key,
        "scope": SHOPIFY_SCOPES,
        "redirect_uri": redirect_uri,
        "state": state,
        "grant_options[]": "per-user",
    }
    oauth_url = f"https://{shop}/admin/oauth/authorize?" + urllib.parse.urlencode(params)
    logger.info("Redirecting %s to Shopify OAuth", shop)
    return RedirectResponse(url=oauth_url)


# ── OAuth Callback ─────────────────────────────────────────────────────────────

@router.get("/shopify/callback")
async def shopify_callback(
    shop: str,
    code: str,
    state: str,
    hmac: str,
    request: Request,
    timestamp: Optional[str] = None,
):
    """
    Step 2 — Handle OAuth callback from Shopify.
    Set this as an Allowed Redirect URL in Shopify Partners:
      https://your-backend/api/v1/shopify/callback
    """
    # Verify HMAC
    raw_params = dict(request.query_params)
    if not _verify_shopify_hmac(raw_params, _settings.shopify_api_secret):
        return JSONResponse(status_code=403, content={"error": "HMAC verification failed"})

    # Verify state (CSRF protection)
    redis = getattr(request.app.state, "redis", None)
    if redis:
        stored_shop = await redis.get(f"shopify:oauth:state:{state}")
        if not stored_shop or stored_shop != shop:
            return JSONResponse(status_code=403, content={"error": "Invalid state token"})
        await redis.delete(f"shopify:oauth:state:{state}")

    # Exchange code for access token
    token_url = f"https://{shop}/admin/oauth/access_token"
    async with httpx.AsyncClient(timeout=15.0) as client:
        token_resp = await client.post(token_url, json={
            "client_id": _settings.shopify_api_key,
            "client_secret": _settings.shopify_api_secret,
            "code": code,
        })

    if token_resp.status_code != 200:
        logger.error("Token exchange failed: %s", token_resp.text)
        return JSONResponse(status_code=502, content={"error": "Token exchange failed"})

    token_data = token_resp.json()
    access_token = token_data["access_token"]
    scope = token_data.get("scope", "")
    logger.info("OAuth complete for %s — scopes: %s", shop, scope)

    # Save tenant to DB
    try:
        from ....core.database import AsyncSessionLocal
        from ....modules.tenants.models import Tenant
        import uuid

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Tenant).where(Tenant.shopify_domain == shop))
            tenant = result.scalar_one_or_none()

            if tenant:
                tenant.shopify_access_token = access_token
                tenant.shopify_scope = scope
                tenant.shopify_installed_at = datetime.now(timezone.utc)
                tenant.is_active = True
            else:
                tenant = Tenant(
                    id=str(uuid.uuid4()),
                    name=shop.replace(".myshopify.com", "").title(),
                    email=f"owner@{shop}",
                    shopify_domain=shop,
                    shopify_access_token=access_token,
                    shopify_scope=scope,
                    shopify_installed_at=datetime.now(timezone.utc),
                )
                db.add(tenant)

            await db.commit()
            logger.info("Tenant saved for %s", shop)
    except Exception as exc:
        logger.error("Failed to save tenant for %s: %s", shop, exc)

    # Register script tag
    backend_url = _get_backend_url(request)
    loader_url = f"{backend_url}/api/v1/shopify/widget-loader.js?shop={shop}"
    try:
        tag = await _register_script_tag(shop, access_token, loader_url)
        logger.info("Script tag registered for %s: id=%s", shop, tag.get("id"))
    except Exception as exc:
        logger.error("Script tag registration failed for %s: %s", shop, exc)

    # Redirect to success page
    return HTMLResponse(content=_success_page(shop, backend_url), status_code=200)


def _success_page(shop: str, backend_url: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
  <title>Aria Installed!</title>
  <style>
    body {{ font-family: -apple-system, sans-serif; display: flex; align-items: center;
           justify-content: center; min-height: 100vh; margin: 0; background: #f4f6f8; }}
    .card {{ background: white; padding: 48px; border-radius: 12px; text-align: center;
             box-shadow: 0 2px 16px rgba(0,0,0,0.1); max-width: 480px; }}
    h1 {{ color: #1a1a2e; margin-bottom: 8px; }}
    p {{ color: #666; line-height: 1.6; }}
    .badge {{ display: inline-block; background: #22c55e; color: white; padding: 6px 16px;
              border-radius: 20px; font-size: 14px; margin-bottom: 24px; }}
    a {{ color: #6366f1; text-decoration: none; font-weight: 600; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="badge">✓ Installed Successfully</div>
    <h1>Aria is live on your store!</h1>
    <p>The AI shopping assistant widget has been added to <strong>{shop}</strong>.</p>
    <p style="margin-top:16px">
      Visit your store to see Aria in action →
      <a href="https://{shop}" target="_blank">Open Store</a>
    </p>
    <p style="margin-top:8px; font-size:13px; color:#999">
      Widget loads from: {backend_url}/static/wooagent-widget.js
    </p>
  </div>
</body>
</html>"""


# ── Widget Loader JS ──────────────────────────────────────────────────────────

@router.get("/shopify/widget-loader.js")
async def widget_loader(request: Request, shop: Optional[str] = None):
    """
    Dynamic JS loader — registered as the Shopify Script Tag.
    Reads per-tenant config from DB when shop param is provided.
    """
    backend_url = _get_backend_url(request)

    # Load per-tenant name from DB; no global fallback (STORE_NAME removed from settings)
    store_name = ""
    currency = _settings.store_currency

    if shop:
        try:
            from ....core.database import AsyncSessionLocal
            from ....modules.tenants.models import Tenant

            async with AsyncSessionLocal() as db:
                result = await db.execute(select(Tenant).where(Tenant.shopify_domain == shop))
                tenant = result.scalar_one_or_none()
                if tenant:
                    store_name = tenant.name or ""
        except Exception:
            pass

    primary_color = os.getenv("SHOPIFY_WIDGET_COLOR", "#6366f1")
    greeting = os.getenv("SHOPIFY_GREETING", "Hi! I'm Aria, your AI shopping assistant. Ask me anything!")

    config = {
        "agent_api_url": backend_url,
        "store_name": store_name,
        "currency": currency,
        "primary_color": primary_color,
        "widget_position": os.getenv("SHOPIFY_WIDGET_POSITION", "bottom-right"),
        "greeting_message": greeting,
        "enable_voice": os.getenv("SHOPIFY_ENABLE_VOICE", "true").lower() != "false",
        "enable_text": os.getenv("SHOPIFY_ENABLE_TEXT", "true").lower() != "false",
        "language": os.getenv("SHOPIFY_LANGUAGE", "en"),
        "platform": "shopify",
        "shop": shop or "",
    }

    import json
    config_json = json.dumps(config, ensure_ascii=False)

    # Inline the widget JS so only ONE request goes to ngrok (avoids CORS/interstitial issues)
    widget_js = ""
    for candidate in ["/app/static/wooagent-widget.js", "static/wooagent-widget.js"]:
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                widget_js = f.read()
            break
        except FileNotFoundError:
            continue

    js = f"""/* Aria Shopping Assistant — inlined loader */
if (!window.__aria_loaded) {{
  window.__aria_loaded = true;
  window.wooagent_config = {config_json};
  {widget_js}
}}
"""
    return Response(
        content=js,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
    )


# ── Manual Setup (dev/testing) ─────────────────────────────────────────────────

class SetupRequest(BaseModel):
    backend_url: Optional[str] = None
    shop: Optional[str] = None


@router.post("/shopify/setup")
async def manual_setup(payload: SetupRequest, request: Request):
    """
    Manual script tag registration for testing without OAuth.
    Requires SHOPIFY_ADMIN_TOKEN and SHOPIFY_STORE_DOMAIN in .env.
    """
    if not _settings.shopify_admin_token:
        return JSONResponse(status_code=400, content={"error": "SHOPIFY_ADMIN_TOKEN not set in .env"})
    if not _settings.shopify_store_domain:
        return JSONResponse(status_code=400, content={"error": "SHOPIFY_STORE_DOMAIN not set in .env"})

    backend_url = _get_backend_url(request, payload.backend_url)
    shop = payload.shop or _settings.shopify_store_domain
    # Don't add ?shop= — Shopify automatically appends it to script tag URLs
    loader_url = f"{backend_url}/api/v1/shopify/widget-loader.js"

    try:
        tag = await _register_script_tag(shop, _settings.shopify_admin_token, loader_url)
    except Exception as exc:
        return JSONResponse(status_code=502, content={"error": str(exc)})

    return {
        "success": True,
        "message": "Widget registered. Visit your store to see Aria.",
        "script_tag_id": tag.get("id"),
        "loader_url": loader_url,
        "widget_js_url": f"{backend_url}/static/wooagent-widget.js",
        "next_steps": [
            f"Visit https://{shop} to see the widget",
            "Hard-refresh if not visible: Ctrl+Shift+R",
            f"For full SaaS OAuth app: set App URL to {backend_url}/api/v1/shopify/install",
        ],
    }


# ── Script Tag Management ─────────────────────────────────────────────────────

@router.get("/shopify/script-tags")
async def list_script_tags(request: Request):
    if not _settings.shopify_admin_token:
        return JSONResponse(status_code=400, content={"error": "SHOPIFY_ADMIN_TOKEN not set"})

    url = f"https://{_settings.shopify_store_domain}/admin/api/{_settings.shopify_api_version}/script_tags.json"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers=_shopify_admin_headers(_settings.shopify_admin_token))

    if resp.status_code != 200:
        return JSONResponse(status_code=502, content={"error": resp.text})

    tags = resp.json().get("script_tags", [])
    return {"count": len(tags), "script_tags": tags}


@router.delete("/shopify/script-tags/{tag_id}")
async def delete_script_tag(tag_id: int, request: Request):
    if not _settings.shopify_admin_token:
        return JSONResponse(status_code=400, content={"error": "SHOPIFY_ADMIN_TOKEN not set"})

    url = f"https://{_settings.shopify_store_domain}/admin/api/{_settings.shopify_api_version}/script_tags/{tag_id}.json"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.delete(url, headers=_shopify_admin_headers(_settings.shopify_admin_token))

    if resp.status_code not in (200, 204):
        return JSONResponse(status_code=502, content={"error": resp.text})

    return {"success": True, "deleted_tag_id": tag_id}
