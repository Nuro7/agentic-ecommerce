"""
Voice WebSocket endpoint.

All voice logic lives in agent/voice/pipelines/.
This file is only responsible for:
  1. Token validation (HMAC)
  2. Tenant resolution → per-tenant store client
  3. Getting the PipelineRouter from app.state
  4. Delegating the session
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from ...agent.gemini_client import (
    _WS_TOKEN_TTL,
    generate_ws_token,
    validate_ws_token,
)
from ...agent.voice.pipelines import PipelineRouter
from ...core.database import AsyncSessionLocal
from ...core.ratelimit import check_rate_limit
from ...modules.billing.dependencies import enforce_conversation_quota, is_voice_allowed
from ...modules.billing.service import BillingService
from ...modules.tenants.dependencies import resolve_tenant_store_client_for_ws
from ...modules.tenants.repository import TenantRepository

logger = logging.getLogger(__name__)
router = APIRouter(tags=["voice"])

# Module-level router singleton — initialised on first connection.
# store_client is NOT stored here — it's passed per run() call.
_pipeline_router: PipelineRouter | None = None


def _get_pipeline_router(app_state) -> PipelineRouter:
    global _pipeline_router
    if _pipeline_router is None:
        session_service = getattr(app_state, "session_service", None)
        _pipeline_router = PipelineRouter(session_service=session_service)
        logger.info("PipelineRouter initialised")
    return _pipeline_router


# ── REST: issue a short-lived WS token ───────────────────────────────────────

@router.get("/wooagent/ws-token")
async def get_ws_token(session_id: str = Query(..., min_length=4, max_length=128)):
    token = generate_ws_token(session_id)
    return {"token": token, "ttl": _WS_TOKEN_TTL}


# ── Health: pipeline status ───────────────────────────────────────────────────

@router.get("/wooagent/pipeline-health")
async def pipeline_health():
    if _pipeline_router is None:
        return {"status": "not_initialised"}
    return {"status": "ok", **_pipeline_router.health()}


# ── WebSocket ─────────────────────────────────────────────────────────────────

@router.websocket("/wooagent/stream")
async def voice_stream(websocket: WebSocket):
    """
    Voice WebSocket — resolves tenant, then delegates to PipelineRouter.

    Query params:
      session_id  — required
      token       — HMAC token from /wooagent/ws-token
      shop        — Shopify domain (e.g. mystore.myshopify.com) for tenant resolution
      tenant_id   — UUID fallback for non-Shopify tenants

    Browser → backend (binary):  PCM Int16 16kHz mono (AudioWorklet)
    Browser → backend (text):    {"type":"text_input","text":"..."}

    Backend → browser (binary):  PCM 16-bit 24kHz mono (Gemini TTS)
    Backend → browser (text):    {"type":"transcript",       "text":"..."}
                                 {"type":"user_transcript",  "text":"..."}
                                 {"type":"ui_action",        "action":{...}}
                                 {"type":"suggestions",      "items":[...]}
                                 {"type":"flush_audio"}
                                 {"type":"turn_complete"}
                                 {"type":"pipeline_error",   "message":"..."}
                                 {"type":"pipeline_fallback","message":"..."}
    """
    session_id = (websocket.query_params.get("session_id") or "").strip()
    token      = websocket.query_params.get("token", "")
    shop       = websocket.query_params.get("shop", "").strip()
    tenant_id  = websocket.query_params.get("tenant_id", "").strip()

    # A missing session_id must NOT collapse to a shared key (e.g. "anonymous"):
    # session state (history, customer_email, cart) is keyed by session_id, so a
    # shared default would leak one caller's data to every other caller without one.
    if len(session_id) < 8:
        await websocket.close(code=4003, reason="Missing or invalid session_id")
        logger.warning("WebSocket rejected — missing session_id (ip=%s)",
                       websocket.client.host if websocket.client else "unknown")
        return

    if not validate_ws_token(token, session_id):
        await websocket.close(code=4003, reason="Invalid or expired token")
        logger.warning("WebSocket rejected — bad token: session=%s", session_id)
        return

    # Connection rate limit: voice sessions are the most expensive path
    # (3 credits + LLM/STT/TTS). Cap new connections per (tenant, IP).
    _redis = getattr(websocket.app.state, "redis", None)
    _ip = websocket.client.host if websocket.client else "unknown"
    if not await check_rate_limit(
        _redis, tenant_key=(shop or tenant_id or session_id), ip=_ip,
        limit=10, window=60, scope="voice",
    ):
        await websocket.close(code=4029, reason="Rate limit exceeded")
        logger.warning("WebSocket rejected — rate limit: session=%s ip=%s", session_id, _ip)
        return

    await websocket.accept()
    logger.info("Voice WebSocket accepted: session=%s shop=%s", session_id, shop or tenant_id or "global")

    try:
        # Resolve per-tenant store client + check billing quota.
        async with AsyncSessionLocal() as db:
            store_client, resolved_tenant_id = await resolve_tenant_store_client_for_ws(
                shop=shop,
                tenant_id=tenant_id,
                app_state=websocket.app.state,
                db=db,
            )
            # Default to voice; tenants whose plan lacks voice fall back to a
            # text-only session instead of being rejected — otherwise a free/Starter
            # merchant's widget (which only talks to this WS) can't chat at all.
            voice_enabled = True
            if resolved_tenant_id:
                voice_enabled = await is_voice_allowed(resolved_tenant_id, db)
                try:
                    await enforce_conversation_quota(
                        resolved_tenant_id, db, is_voice=voice_enabled,
                        redis=getattr(websocket.app.state, "redis", None),
                    )
                except HTTPException as quota_err:
                    # Real quota exhaustion (not the voice gate, which is bypassed
                    # for text mode) — surface and close.
                    await websocket.send_text(json.dumps({
                        "type": "pipeline_error",
                        "message": quota_err.detail,
                        "code": quota_err.status_code,
                    }))
                    await websocket.close(code=4029)
                    logger.info(
                        "Voice WebSocket closed — quota exhausted: tenant=%s session=%s",
                        resolved_tenant_id, session_id,
                    )
                    return
                try:
                    # Voice sessions cost 3 credits; text-only sessions cost 1.
                    await BillingService(db).record_usage(
                        resolved_tenant_id, "credits", 3 if voice_enabled else 1)
                    await db.commit()
                except Exception as exc:
                    logger.warning(
                        "Failed to record session credits: tenant=%s: %s",
                        resolved_tenant_id, exc,
                    )

        pipeline_router = _get_pipeline_router(websocket.app.state)
        await pipeline_router.run(
            websocket, session_id, store_client=store_client, voice_enabled=voice_enabled)

    except WebSocketDisconnect:
        logger.info("Client disconnected: session=%s", session_id)
    except Exception as e:
        logger.error(
            "Voice stream error session=%s: %s: %s",
            session_id, type(e).__name__, e,
            exc_info=True,
        )
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
