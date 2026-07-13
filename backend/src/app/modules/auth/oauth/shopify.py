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

# Admin scopes + unauthenticated (Storefront) scopes. The unauthenticated_* scopes
# let us mint a working Storefront access token via storefrontAccessTokenCreate after
# install, so merchants never have to create one manually. NOTE: these same scopes
# must also be enabled in the Shopify Partner app configuration (one-time, per app).
SHOPIFY_SCOPES = (
    "read_products,write_script_tags,read_script_tags,read_orders,read_customers,"
    "unauthenticated_read_product_listings,unauthenticated_read_product_inventory,"
    "unauthenticated_read_product_tags,unauthenticated_read_checkouts,"
    "unauthenticated_write_checkouts"
)


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


async def _fetch_shop_info(shop_domain: str, access_token: str) -> Optional[dict]:
    """Fetch shop.json — doubles as the Admin-token validity check AND the source of
    real shop metadata (name, email, currency). Returns the parsed `shop` object on
    success, None on failure (token invalid / network error)."""
    api_version = _settings.shopify_api_version
    url = f"https://{shop_domain}/admin/api/{api_version}/shop.json"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=_shopify_admin_headers(access_token))
        if resp.status_code != 200:
            return None
        return resp.json().get("shop") or {}
    except Exception as exc:
        logger.error("shop.json fetch failed for %s: %s", shop_domain, exc)
        return None


# ISO currency code → display symbol for the widget/prompts. Unknown codes fall
# back to the code itself (still correct, just less pretty than a symbol).
_CURRENCY_SYMBOLS = {
    "INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "AED": "د.إ",
    "JPY": "¥", "CNY": "¥", "AUD": "A$", "CAD": "C$", "SGD": "S$",
    "SAR": "﷼", "BDT": "৳", "LKR": "Rs", "PKR": "Rs", "NZD": "NZ$",
}


def _currency_symbol_for(code) -> Optional[str]:
    if not code:
        return None
    normalized = str(code).strip().upper()
    return _CURRENCY_SYMBOLS.get(normalized, normalized)


# Webhook topics Speako subscribes to on install. The receiving endpoints already
# exist (modules/webhooks): products/* keep product_cache in sync; app/uninstalled
# lets us deactivate the tenant.
SHOPIFY_WEBHOOK_TOPICS = (
    "products/create",
    "products/update",
    "products/delete",
    "app/uninstalled",
)


def _build_webhook_payload(topic: str, address: str) -> dict:
    """Pure payload builder (kept separate so it can be unit-tested without httpx)."""
    return {"webhook": {"topic": topic, "address": address, "format": "json"}}


async def _register_webhooks(shop_domain: str, access_token: str, address: str) -> int:
    """Idempotently subscribe the store to SHOPIFY_WEBHOOK_TOPICS pointing at
    `address`. Skips topics already registered to our address; tolerates Shopify's
    422 "address for this topic has already been taken". Returns the number of
    topics confirmed active (existing + newly created). Never raises."""
    api_version = _settings.shopify_api_version
    base = f"https://{shop_domain}/admin/api/{api_version}/webhooks.json"
    headers = _shopify_admin_headers(access_token)
    confirmed = 0

    async with httpx.AsyncClient(timeout=15.0) as client:
        existing: set = set()
        try:
            resp = await client.get(base, headers=headers, params={"limit": 250})
            if resp.status_code == 200:
                for wh in resp.json().get("webhooks", []):
                    if str(wh.get("address", "")).rstrip("/") == address.rstrip("/"):
                        existing.add(str(wh.get("topic", "")))
        except Exception as exc:
            logger.warning("Could not list existing webhooks for %s: %s", shop_domain, exc)

        for topic in SHOPIFY_WEBHOOK_TOPICS:
            if topic in existing:
                confirmed += 1
                continue
            try:
                resp = await client.post(
                    base, headers=headers, json=_build_webhook_payload(topic, address)
                )
                if resp.status_code in (200, 201):
                    confirmed += 1
                    logger.info("Webhook registered for %s: %s", shop_domain, topic)
                elif resp.status_code == 422 and "taken" in resp.text.lower():
                    confirmed += 1  # duplicate (topic+address already subscribed)
                else:
                    logger.warning(
                        "Webhook %s registration failed for %s: %s %s",
                        topic, shop_domain, resp.status_code, resp.text[:200],
                    )
            except Exception as exc:
                logger.warning("Webhook %s registration error for %s: %s", topic, shop_domain, exc)

    return confirmed


async def _create_storefront_token(shop_domain: str, admin_token: str) -> str:
    """Mint a Storefront API access token via the Admin API, using the Admin token we
    just got from OAuth. This is what lets merchants install with ONE click and never
    create/paste a Storefront token themselves. Returns "" on failure (the Admin-API
    product fallback still keeps things working)."""
    api_version = _settings.shopify_api_version or "2024-01"
    url = f"https://{shop_domain}/admin/api/{api_version}/graphql.json"
    mutation = (
        'mutation { storefrontAccessTokenCreate(input: {title: "Speako"}) '
        '{ storefrontAccessToken { accessToken } userErrors { field message } } }'
    )
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                url,
                json={"query": mutation},
                headers=_shopify_admin_headers(admin_token),
            )
        resp.raise_for_status()
        body = resp.json()
        node = ((body.get("data") or {}).get("storefrontAccessTokenCreate") or {})
        errors = node.get("userErrors") or body.get("errors")
        if errors:
            logger.warning("storefrontAccessTokenCreate errors for %s: %s", shop_domain, errors)
        token = ((node.get("storefrontAccessToken") or {}).get("accessToken") or "")
        if token:
            logger.info("Storefront token provisioned for %s", shop_domain)
        return token
    except Exception as exc:
        logger.warning("Failed to provision Storefront token for %s: %s", shop_domain, exc)
        return ""


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

    # NOTE: no "grant_options[]": "per-user" — we want an OFFLINE token. Per-user
    # (online) tokens expire ~24h, which would silently disconnect the store every
    # day. An offline token persists until the app is uninstalled, which is what a
    # server-side SaaS that fetches products around the clock needs.
    params = {
        "client_id": _settings.shopify_api_key,
        "scope": SHOPIFY_SCOPES,
        "redirect_uri": redirect_uri,
        "state": state,
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

    backend_url = _get_backend_url(request)

    # Verify the token actually works against the Admin API BEFORE we declare success.
    # A token that can't read the shop is useless — better to fail loudly here than to
    # show a green "Installed!" page and have the store silently broken at search time.
    # The same shop.json response also gives us the REAL shop metadata (name, email,
    # currency) instead of fabricating it from the domain string.
    shop_info = await _fetch_shop_info(shop, access_token)
    if shop_info is None:
        logger.error("Admin token verification FAILED for %s (shop.json not readable)", shop)
        return HTMLResponse(
            content=_error_page(shop, "Couldn't verify the access token with Shopify (shop.json was not readable). Please try installing again."),
            status_code=502,
        )

    # Auto-provision a Storefront token so the merchant never has to create one.
    # On re-install this mints a fresh token (handles revoked/old-app tokens).
    storefront_token = await _create_storefront_token(shop, access_token)

    # Best-effort: fetch the store's real shipping/returns policies + payment methods
    # via the Storefront API so we can PREFILL the tenant's editable config columns
    # (import-then-edit). Non-fatal — install proceeds fine without them.
    imported_policies: dict = {}
    if storefront_token:
        try:
            from ....integrations.shopify.client import ShopifyClient
            _sc = ShopifyClient(
                store_domain=shop,
                storefront_token=storefront_token,
                admin_token=access_token,
            )
            _pol = await _sc.get_store_policies()
            if _pol.get("success"):
                imported_policies = {
                    "shipping_policy": (_pol.get("shipping_policy") or "").strip() or None,
                    "returns_policy": (_pol.get("returns_policy") or "").strip() or None,
                    "payment_methods": ", ".join(_pol.get("payment_methods") or []) or None,
                }
        except Exception as exc:
            logger.warning("Policy prefill fetch failed for %s: %s", shop, exc)

    # Save tenant to DB. A failure here MUST surface — otherwise the merchant sees
    # "Installed!" while no usable token was persisted (the exact silent bug we hit).
    try:
        from ....core.database import AsyncSessionLocal
        from ....modules.tenants.models import Tenant
        import uuid

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Tenant).where(Tenant.shopify_domain == shop))
            tenant = result.scalar_one_or_none()

            # Real shop metadata from shop.json (was previously discarded).
            real_name = str(shop_info.get("name") or "").strip()
            real_email = str(shop_info.get("email") or "").strip().lower()
            real_currency = _currency_symbol_for(shop_info.get("currency"))

            if tenant:
                tenant.shopify_access_token = access_token
                tenant.shopify_scope = scope
                tenant.shopify_installed_at = datetime.now(timezone.utc)
                tenant.is_active = True
                # Only overwrite when we successfully minted a new one.
                if storefront_token:
                    tenant.shopify_storefront_token = storefront_token
                # Fill config from Shopify ONLY where unset — never clobber values
                # the merchant configured (import-then-edit contract).
                if not tenant.currency_symbol and real_currency:
                    tenant.currency_symbol = real_currency
                for _field, _value in imported_policies.items():
                    if _value and getattr(tenant, _field, None) in (None, ""):
                        setattr(tenant, _field, _value)
            else:
                # New tenant: prefer real shop email, but it must not collide with an
                # existing account (email is UNIQUE) — fall back to the synthetic one.
                email = real_email or f"owner@{shop}"
                if real_email:
                    dup = await db.execute(select(Tenant).where(Tenant.email == real_email))
                    if dup.scalar_one_or_none() is not None:
                        email = f"owner@{shop}"
                tenant = Tenant(
                    id=str(uuid.uuid4()),
                    name=real_name or shop.replace(".myshopify.com", "").title(),
                    email=email,
                    shopify_domain=shop,
                    shopify_access_token=access_token,
                    shopify_storefront_token=storefront_token or None,
                    shopify_scope=scope,
                    shopify_installed_at=datetime.now(timezone.utc),
                    currency_symbol=real_currency,
                    **{k: v for k, v in imported_policies.items() if v},
                )
                db.add(tenant)

            tenant_id = tenant.id  # capture before commit (expire_on_commit)
            await db.commit()
            logger.info(
                "Tenant saved for %s — admin=yes storefront=%s name=%r currency=%s policies_prefilled=%s",
                shop, "yes" if storefront_token else "no",
                real_name or "(domain-derived)", real_currency or "-",
                sorted(k for k, v in imported_policies.items() if v) or "none",
            )
    except Exception as exc:
        logger.error("Failed to save tenant for %s: %s", shop, exc, exc_info=True)
        return HTMLResponse(
            content=_error_page(shop, f"The store was authorized, but saving its credentials to the database failed ({type(exc).__name__}). Check the server logs and DATABASE_URL, then re-install."),
            status_code=500,
        )

    # Queue the initial product sync (non-blocking — Celery). Without this the
    # store's product_cache stays empty until the nightly worker run; searches
    # would fall back to the slow live API for up to 24h. Mirrors onboarding.py.
    try:
        from ....workers.tasks.sync_products import sync_products
        sync_products.delay(tenant_id=tenant_id)
        logger.info("Initial product sync queued for tenant=%s (%s)", tenant_id, shop)
    except Exception as exc:
        logger.warning(
            "Could not queue product sync for %s (Celery unavailable): %s", shop, exc,
        )

    # Register product/uninstall webhooks so Shopify actually SENDS the events our
    # /webhooks/shopify/{tenant_id} receiver is built to handle. Non-fatal.
    webhook_address = f"{backend_url}/api/v1/webhooks/shopify/{tenant_id}"
    try:
        registered = await _register_webhooks(shop, access_token, webhook_address)
        logger.info(
            "Webhooks registered for %s: %d/%d topics",
            shop, registered, len(SHOPIFY_WEBHOOK_TOPICS),
        )
    except Exception as exc:
        logger.error("Webhook registration failed for %s: %s", shop, exc)

    # Register script tag (non-fatal — the install itself already succeeded).
    loader_url = f"{backend_url}/api/v1/shopify/widget-loader.js?shop={shop}"
    try:
        tag = await _register_script_tag(shop, access_token, loader_url)
        logger.info("Script tag registered for %s: id=%s", shop, tag.get("id"))
    except Exception as exc:
        logger.error("Script tag registration failed for %s: %s", shop, exc)

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


def _error_page(shop: str, reason: str) -> str:
    """Shown when install authorized but did NOT fully succeed — so the merchant
    sees the real failure instead of a misleading green 'Installed!' page."""
    return f"""<!DOCTYPE html>
<html>
<head>
  <title>Install incomplete</title>
  <style>
    body {{ font-family: -apple-system, sans-serif; display: flex; align-items: center;
           justify-content: center; min-height: 100vh; margin: 0; background: #f4f6f8; }}
    .card {{ background: white; padding: 48px; border-radius: 12px; text-align: center;
             box-shadow: 0 2px 16px rgba(0,0,0,0.1); max-width: 480px; }}
    h1 {{ color: #1a1a2e; margin-bottom: 8px; }}
    p {{ color: #666; line-height: 1.6; }}
    .badge {{ display: inline-block; background: #ef4444; color: white; padding: 6px 16px;
              border-radius: 20px; font-size: 14px; margin-bottom: 24px; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="badge">✕ Install not completed</div>
    <h1>Couldn't finish connecting {shop}</h1>
    <p>{reason}</p>
    <p style="margin-top:16px; font-size:13px; color:#999">
      If this keeps happening, check the server logs for this store's domain.
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

    # Load per-tenant name/config from DB; no global fallback (STORE_NAME removed from settings)
    store_name = ""
    currency = _settings.store_currency
    tenant_greeting = ""

    if shop:
        try:
            from ....core.database import AsyncSessionLocal
            from ....modules.tenants.models import Tenant

            async with AsyncSessionLocal() as db:
                result = await db.execute(select(Tenant).where(Tenant.shopify_domain == shop))
                tenant = result.scalar_one_or_none()
                if tenant:
                    store_name = tenant.name or ""
                    tenant_greeting = (tenant.greeting_message or "").strip()
                    if tenant.currency_symbol:
                        currency = tenant.currency_symbol
        except Exception:
            pass

    primary_color = os.getenv("SHOPIFY_WIDGET_COLOR", "#6366f1")
    # Merchant-customized greeting (tenant column) → env → default.
    greeting = tenant_greeting or os.getenv(
        "SHOPIFY_GREETING", "Hi! I'm Aria, your AI shopping assistant. Ask me anything!"
    )

    config = {
        "agent_api_url": backend_url,
        "store_name": store_name,
        "currency": currency,
        "primary_color": primary_color,
        "widget_position": os.getenv("SHOPIFY_WIDGET_POSITION", "bottom-right"),
        "greeting_message": greeting,
        "enable_voice": os.getenv("SHOPIFY_ENABLE_VOICE", "true").lower() != "false",
        "enable_text": os.getenv("SHOPIFY_ENABLE_TEXT", "true").lower() != "false",
        # Live Shopping Navigator: agent drives the storefront (search/product/cart)
        "live_navigation": os.getenv("SHOPIFY_LIVE_NAV", "true").lower() != "false",
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
