"""
Pipeline Router — Active Pipeline Selector with Circuit Breaker + Health Monitor.
Routes each voice WebSocket session through the available provider cascade.

Each provider has its own circuit breaker (2 failures → 30s cooldown).
Every start, failure, and fallback is logged with [pipeline] prefix for
easy grepping in production logs.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .pipeline_c import PipelineC
from ....modules.tenants.dependencies import DEV_TENANT_ID
from ....config import settings

# Import Turn Coordinator and Provider Abstractions
from ..providers.gemini_live import GeminiLiveProvider
from ..providers.openai_realtime import OpenAIVoiceProvider
from ..coordinator import VoiceTurnCoordinator

# Shared Circuit Breaker implementation
from ....core.circuit_breaker import CircuitBreaker  # noqa: E402

logger = logging.getLogger(__name__)


class PipelineRouter:
    """
    Routes each voice WebSocket session through the provider cascade.

    Default (voice_provider=gemini_live):
      Gemini Live (PRIMARY) → OpenAI Realtime (FALLBACK) → Text (DEGRADED)

    With voice_provider=openai:
      OpenAI Realtime (PRIMARY) → Gemini Live (FALLBACK) → Text (DEGRADED)

    Instantiate once at startup and store in app.state.pipeline_router.
    """

    def __init__(self, session_service: Any) -> None:
        self._session_service = session_service
        self._pipeline_c = PipelineC(session_service)

        self._gemini_primary = (settings.voice_provider or "gemini_live").lower().strip() != "openai"

        self._breaker_a = CircuitBreaker(
            name="A", failure_threshold=2, recovery_timeout=30.0
        )
        self._breaker_b = CircuitBreaker(
            name="B", failure_threshold=2, recovery_timeout=30.0
        )

    # ── Routing ───────────────────────────────────────────────────────────────

    async def run(self, websocket: Any, session_id: str, store_client: Any = None,
                  voice_enabled: bool = True, tenant_id: str = DEV_TENANT_ID) -> None:
        """
        Route this session through the best available pipeline/provider.
        Cascade order follows `settings.voice_provider`:
          gemini_live (default) → Gemini Live (primary) → OpenAI Realtime (fallback) → Text.
          openai                → OpenAI Realtime (primary) → Gemini Live (fallback) → Text.
        Logs every pipeline start, failure, and fallback so operators can see which
        model is active per session.
        """
        provider_choice = (settings.voice_provider or "gemini_live").lower().strip()

        # ── Helper: try a single provider, return True on success ─────────────
        async def _try_provider(
            name: str,
            provider_cls: Any,
            breaker: CircuitBreaker | None,
            fallback_name: str,
        ) -> bool:
            if breaker and not breaker.is_available():
                cooldown = breaker.health().get("recovery_in", 0)
                logger.warning(
                    "[pipeline] %s breaker OPEN — skipping (cooldown %.0fs): session=%s",
                    name, cooldown, session_id,
                )
                return False
            logger.info("[pipeline] Starting %s: session=%s", name, session_id)
            try:
                provider = provider_cls(self._session_service)
                coordinator = VoiceTurnCoordinator(websocket, provider, self._session_service)
                await coordinator.run(session_id, store_client, tenant_id)
                if breaker:
                    breaker.record_success()
                logger.info("[pipeline] %s completed successfully: session=%s", name, session_id)
                return True
            except Exception as exc:
                if breaker:
                    breaker.record_failure()
                logger.error(
                    "[pipeline] %s FAILED session=%s | error=%s: %s%s",
                    name, session_id, type(exc).__name__, exc,
                    f" | breaker={breaker.state}" if breaker else "",
                    exc_info=True,
                )
                await self._notify_fallback(
                    websocket, from_pipeline=name, to_pipeline=fallback_name,
                    message=f"{name} unavailable. Trying {fallback_name}...",
                )
                return False

        # ── Cascade: primary provider follows voice_provider setting ───────────
        if provider_choice == "openai":
            cascade = [
                ("OpenAI Realtime", OpenAIVoiceProvider, self._breaker_a, "Gemini Live"),
                ("Gemini Live", GeminiLiveProvider, self._breaker_b, "Text"),
            ]
        else:
            cascade = [
                ("Gemini Live", GeminiLiveProvider, self._breaker_b, "OpenAI Realtime"),
                ("OpenAI Realtime", OpenAIVoiceProvider, self._breaker_a, "Text"),
            ]

        for name, cls, breaker, fb_name in cascade:
            ok = await _try_provider(name, cls, breaker, fb_name)
            if ok:
                return

        # ── Pipeline C (Text Fallback) ──
        logger.warning("[pipeline] All voice providers failed, falling back to Text: session=%s", session_id)
        await self._run_text_fallback(websocket, session_id, store_client, tenant_id)

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _run_text_fallback(self, websocket: Any, session_id: str, store_client: Any, tenant_id: str) -> None:
        try:
            await self._pipeline_c.run(websocket, session_id, store_client, tenant_id)
        except Exception as exc:
            logger.error("Pipeline C (text fallback) failed session=%s: %s: %s",
                         session_id, type(exc).__name__, exc)
            try:
                await websocket.send_text(json.dumps({
                    "type": "error", "message": "Service unavailable. Please refresh."}))
                await websocket.close(code=1011)
            except Exception:
                pass

    @staticmethod
    async def _notify_fallback(
        websocket: Any,
        from_pipeline: str,
        to_pipeline: str,
        message: str,
    ) -> None:
        try:
            await websocket.send_text(json.dumps({
                "type":          "pipeline_fallback",
                "from_pipeline": from_pipeline,
                "to_pipeline":   to_pipeline,
                "message":       message,
            }))
        except Exception:
            pass

    # ── Status ────────────────────────────────────────────────────────────────

    @property
    def active_pipeline(self) -> str:
        if self._gemini_primary:
            if self._breaker_b.state == "closed":
                return "Gemini Live (primary)"
            if self._breaker_a.state == "closed":
                return "OpenAI Realtime (fallback)"
        else:
            if self._breaker_a.state == "closed":
                return "OpenAI Realtime (primary)"
            if self._breaker_b.state == "closed":
                return "Gemini Live (fallback)"
        return "Text (degraded)"

    def health(self) -> dict:
        if self._gemini_primary:
            openai_desc = "FALLBACK — GPT Realtime voice"
            gemini_desc = "PRIMARY — Gemini Live voice"
            cascade_order = "Gemini Live → OpenAI Realtime → Text"
        else:
            openai_desc = "PRIMARY — GPT Realtime voice"
            gemini_desc = "FALLBACK — Gemini Live voice"
            cascade_order = "OpenAI Realtime → Gemini Live → Text"
        return {
            "active_pipeline": self.active_pipeline,
            "voice_provider_setting": settings.voice_provider,
            "cascade_order": cascade_order,
            "openai_realtime": {
                **self._breaker_a.health(),
                "model": settings.openai_realtime_model,
                "description": openai_desc,
            },
            "gemini_live": {
                **self._breaker_b.health(),
                "model": settings.gemini_live_model,
                "description": gemini_desc,
            },
            "text_fallback": {
                "state":       "always_available",
                "description": "DEGRADED — Text-only mode (no voice)",
            },
        }
