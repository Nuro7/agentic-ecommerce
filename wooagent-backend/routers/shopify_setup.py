"""
routers/shopify_setup.py

Shopify widget embed endpoints.

GET  /shopify/widget-loader.js   — Dynamic JS: sets window.wooagent_config + loads widget
POST /shopify/setup              — One-time: registers the script tag with Shopify Admin API
GET  /shopify/script-tags        — List registered script tags (for verification)
DELETE /shopify/script-tags/{id} — Remove a script tag (cleanup / redeploy)

Usage (run once after deployment):
  POST https://your-backend/shopify/setup
  Body: {"backend_url": "https://abc123.ngrok.io"}
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()


class SetupRequest(BaseModel):
    backend_url: Optional[str] = None  # If omitted, derived from the current request Host


def _get_backend_url(request: Request, override: Optional[str] = None) -> str:
    """
    Resolve the public-facing backend URL in this priority order:
    1. Value passed in the request body (most explicit)
    2. BACKEND_URL env var
    3. X-Forwarded-Proto + Host headers (ngrok / reverse proxy)
    4. request.base_url (direct connection)
    """
    if override:
        return override.rstrip("/")
    env_url = os.getenv("BACKEND_URL", "").strip().rstrip("/")
    if env_url:
        return env_url
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", "localhost"))
    return f"{proto}://{host}"


def _shopify_admin_headers(admin_token: str) -> dict:
    return {
        "X-Shopify-Access-Token": admin_token,
        "Content-Type": "application/json",
    }


@router.get("/shopify/widget-loader.js")
async def widget_loader(request: Request):
    """
    Returns a self-bootstrapping JavaScript snippet.
    Register this URL as a single Shopify Script Tag — it:
      1. Sets window.wooagent_config with env-derived values
      2. Dynamically loads the widget JS from /static/wooagent-widget.js
    """
    settings = getattr(request.app.state, "settings", None)
    backend_url = _get_backend_url(request)

    store_name = (settings.store_name if settings else None) or os.getenv("STORE_NAME", "Store")
    currency = (settings.store_currency if settings else None) or os.getenv("STORE_CURRENCY", "$")
    primary_color = os.getenv("SHOPIFY_WIDGET_COLOR", "#6366f1")
    enable_voice = os.getenv("SHOPIFY_ENABLE_VOICE", "true").lower() != "false"
    enable_text = os.getenv("SHOPIFY_ENABLE_TEXT", "true").lower() != "false"

    config = {
        "agent_api_url": backend_url,
        "store_name": store_name,
        "currency": currency,
        "primary_color": primary_color,
        "widget_position": os.getenv("SHOPIFY_WIDGET_POSITION", "bottom-right"),
        "greeting_message": os.getenv("SHOPIFY_GREETING", "Hi! I'm Aria, your shopping assistant. Ask me anything!"),
        "enable_voice": enable_voice,
        "enable_text": enable_text,
        "language": os.getenv("SHOPIFY_LANGUAGE", "en"),
        "platform": "shopify",
    }

    config_json = json.dumps(config, ensure_ascii=False)
    widget_url = f"{backend_url}/static/wooagent-widget.js"

    js = f"""/* WooAgent Shopify loader — auto-generated */
(function() {{
  if (window.__wooagent_loaded) return;
  window.__wooagent_loaded = true;

  window.wooagent_config = {config_json};

  var s = document.createElement('script');
  s.src = '{widget_url}';
  s.async = true;
  s.crossOrigin = 'anonymous';
  document.head.appendChild(s);
}})();
"""

    return Response(
        content=js,
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Access-Control-Allow-Origin": "*",
        },
    )


@router.post("/shopify/setup")
async def register_script_tag(payload: SetupRequest, request: Request):
    """
    One-time setup: registers the widget-loader.js as a Shopify Script Tag.
    Call this once after each deployment or backend URL change.

    Example:
      curl -X POST https://your-backend/shopify/setup \\
           -H 'Content-Type: application/json' \\
           -d '{"backend_url": "https://abc123.ngrok.io"}'
    """
    settings = getattr(request.app.state, "settings", None)
    if not settings or not settings.shopify_admin_token:
        return JSONResponse(
            status_code=400,
            content={"error": "SHOPIFY_ADMIN_TOKEN is not configured. Set it in .env and restart."},
        )
    if not settings.shopify_store_domain:
        return JSONResponse(
            status_code=400,
            content={"error": "SHOPIFY_STORE_DOMAIN is not configured. Set it in .env and restart."},
        )

    backend_url = _get_backend_url(request, payload.backend_url)
    loader_url = f"{backend_url}/shopify/widget-loader.js"
    api_version = settings.shopify_api_version
    domain = settings.shopify_store_domain
    admin_token = settings.shopify_admin_token

    script_tags_url = f"https://{domain}/admin/api/{api_version}/script_tags.json"

    async with httpx.AsyncClient(timeout=15.0) as client:
        # 1) List existing script tags to avoid duplicates
        list_resp = await client.get(script_tags_url, headers=_shopify_admin_headers(admin_token))
        existing = []
        if list_resp.status_code == 200:
            existing = list_resp.json().get("script_tags", [])

        # Remove any old wooagent loader tags (previous deployments)
        for tag in existing:
            if "wooagent" in str(tag.get("src", "")).lower() or "widget-loader" in str(tag.get("src", "")).lower():
                del_url = f"https://{domain}/admin/api/{api_version}/script_tags/{tag['id']}.json"
                await client.delete(del_url, headers=_shopify_admin_headers(admin_token))
                logger.info("Removed old script tag %s (%s)", tag["id"], tag.get("src"))

        # 2) Register new loader script tag
        create_resp = await client.post(
            script_tags_url,
            headers=_shopify_admin_headers(admin_token),
            json={
                "script_tag": {
                    "event": "onload",
                    "src": loader_url,
                    "display_scope": "online_store",
                }
            },
        )

    if create_resp.status_code not in (200, 201):
        logger.error("Shopify script_tag registration failed: %s", create_resp.text)
        return JSONResponse(
            status_code=502,
            content={
                "error": "Shopify API rejected the script tag registration",
                "shopify_status": create_resp.status_code,
                "detail": create_resp.text[:500],
            },
        )

    tag = create_resp.json().get("script_tag", {})
    logger.info("Shopify script tag registered: id=%s src=%s", tag.get("id"), tag.get("src"))

    return {
        "success": True,
        "message": "Script tag registered. Widget will appear on your Shopify store within ~60 seconds.",
        "script_tag_id": tag.get("id"),
        "loader_url": loader_url,
        "widget_js_url": f"{backend_url}/static/wooagent-widget.js",
        "next_steps": [
            f"1. Visit your Shopify store: https://{domain}",
            "2. You should see the chat widget in the bottom-right corner",
            "3. If not visible after 60s, hard-refresh (Ctrl+Shift+R)",
            f"4. To update backend URL later, re-run POST /shopify/setup with new backend_url",
        ],
    }


@router.get("/shopify/script-tags")
async def list_script_tags(request: Request):
    """List all script tags registered on the Shopify store (for debugging)."""
    settings = getattr(request.app.state, "settings", None)
    if not settings or not settings.shopify_admin_token:
        return JSONResponse(status_code=400, content={"error": "SHOPIFY_ADMIN_TOKEN not configured"})

    url = f"https://{settings.shopify_store_domain}/admin/api/{settings.shopify_api_version}/script_tags.json"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers=_shopify_admin_headers(settings.shopify_admin_token))

    if resp.status_code != 200:
        return JSONResponse(status_code=502, content={"error": resp.text})

    tags = resp.json().get("script_tags", [])
    return {"count": len(tags), "script_tags": tags}


@router.delete("/shopify/script-tags/{tag_id}")
async def delete_script_tag(tag_id: int, request: Request):
    """Remove a specific script tag by ID."""
    settings = getattr(request.app.state, "settings", None)
    if not settings or not settings.shopify_admin_token:
        return JSONResponse(status_code=400, content={"error": "SHOPIFY_ADMIN_TOKEN not configured"})

    url = f"https://{settings.shopify_store_domain}/admin/api/{settings.shopify_api_version}/script_tags/{tag_id}.json"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.delete(url, headers=_shopify_admin_headers(settings.shopify_admin_token))

    if resp.status_code not in (200, 204):
        return JSONResponse(status_code=502, content={"error": resp.text})

    return {"success": True, "deleted_tag_id": tag_id}
