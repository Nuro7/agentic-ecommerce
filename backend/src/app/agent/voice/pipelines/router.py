"""
Pipeline Router — Active Pipeline Selector with Circuit Breaker + Health Monitor

Routing order:
  Pipeline A  →  Gemini 3.1 Flash Live + Brain (GPT-4o-mini → Grok → Gemini)
                 PRIMARY — multilingual, lowest latency, native STT+TTS
                 Circuit breaker: opens after 3 failures, resets after 60s

  Pipeline B  →  xAI Grok STT → Brain (GPT-4o-mini → Grok → Gemini) → Gemini 3.1 Flash TTS
                 FALLBACK — activates when Pipeline A circuit opens
                 Circuit breaker: opens after 3 failures, resets after 120s

  Pipeline C  →  Text-only degraded mode
                 LAST RESORT — no audio, Brain still works via text
                 No circuit breaker (always available)

Circuit Breaker states:
  CLOSED    → pipeline active, normal operation
  OPEN      → failed N times → next pipeline takes over
  HALF_OPEN → recovery probe: try once, reset if it succeeds
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .pipeline_a import PipelineA
from .pipeline_b import PipelineB
from .pipeline_c import PipelineC
from ....modules.tenants.dependencies import DEV_TENANT_ID

logger = logging.getLogger(__name__)


# ── Circuit Breaker (shared implementation) ──────────────────────────────────
from ....core.circuit_breaker import CircuitBreaker  # noqa: E402


# ── Pipeline Router ───────────────────────────────────────────────────────────

class PipelineRouter:
    """
    Routes each voice WebSocket session through the pipeline cascade:
      A → B → C

    Instantiate once at startup and store in app.state.pipeline_router.
    """

    def __init__(self, session_service: Any) -> None:
        # store_client is NOT stored here — it's passed per run() call
        # so each tenant gets their own isolated client instance.
        self._pipeline_a = PipelineA(session_service)
        self._pipeline_b = PipelineB(session_service)
        self._pipeline_c = PipelineC(session_service)

        self._breaker_a = CircuitBreaker(
            name="A", failure_threshold=3, recovery_timeout=60.0
        )
        self._breaker_b = CircuitBreaker(
            name="B", failure_threshold=3, recovery_timeout=120.0
        )
        # Pipeline C has no circuit breaker — it's always available (text only)

    # ── Routing ───────────────────────────────────────────────────────────────

    async def run(self, websocket: Any, session_id: str, store_client: Any = None,
                  voice_enabled: bool = True, tenant_id: str = DEV_TENANT_ID) -> None:
        """
        Route this session through the best available pipeline.
        Tries A → B → C in order, based on circuit breaker state.
        store_client is the per-tenant client resolved by the WebSocket handler.
        voice_enabled=False (plan without voice) → skip the audio pipelines (A/B)
        and serve text-only via Pipeline C, so the assistant still works.
        """
        if not voice_enabled:
            logger.info("Text-only session (plan without voice): session=%s", session_id)
            try:
                await self._pipeline_c.run(websocket, session_id, store_client, tenant_id)
            except Exception as exc:
                logger.error("Pipeline C (text-only) failed session=%s: %s: %s",
                             session_id, type(exc).__name__, exc)
                try:
                    await websocket.send_text(json.dumps({
                        "type": "error", "message": "Service unavailable. Please refresh."}))
                    await websocket.close(code=1011)
                except Exception:
                    pass
            return

        # ── Pipeline A (Gemini Live) ──────────────────────────────────────────
        if self._breaker_a.is_available():
            try:
                await self._pipeline_a.run(websocket, session_id, store_client, tenant_id)
                self._breaker_a.record_success()
                return
            except Exception as exc:
                self._breaker_a.record_failure()
                logger.error(
                    "Pipeline A failed session=%s: %s: %s | breaker=%s",
                    session_id, type(exc).__name__, exc, self._breaker_a.state,
                )
                await self._notify_fallback(
                    websocket, from_pipeline="A", to_pipeline="B",
                    message="Voice service interrupted. Reconnecting...",
                )

        # ── Pipeline B (xAI Grok STT + Gemini TTS) — only if A failed ─────────
        if self._breaker_b.is_available():
            try:
                await self._pipeline_b.run(websocket, session_id, store_client, tenant_id)
                self._breaker_b.record_success()
                return
            except Exception as exc:
                self._breaker_b.record_failure()
                logger.error(
                    "Pipeline B failed session=%s: %s: %s | breaker=%s",
                    session_id, type(exc).__name__, exc, self._breaker_b.state,
                )
                await self._notify_fallback(
                    websocket, from_pipeline="B", to_pipeline="C",
                    message="Switching to text mode.",
                )

        # ── Pipeline C (text-only) — always available ─────────────────────────
        try:
            await self._pipeline_c.run(websocket, session_id, store_client, tenant_id)
        except Exception as exc:
            logger.error(
                "Pipeline C failed session=%s: %s: %s",
                session_id, type(exc).__name__, exc,
            )
            try:
                await websocket.send_text(json.dumps({
                    "type":    "error",
                    "message": "Service unavailable. Please refresh the page.",
                }))
                await websocket.close(code=1011)
            except Exception:
                pass

    # ── Helpers ───────────────────────────────────────────────────────────────

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
        if self._breaker_a.is_available():
            return "A"
        if self._breaker_b.is_available():
            return "B"
        return "C"

    def health(self) -> dict:
        return {
            "active_pipeline": self.active_pipeline,
            "pipeline_a": {
                **self._breaker_a.health(),
                "description": "Gemini 3.1 Flash Live + Brain",
            },
            "pipeline_b": {
                **self._breaker_b.health(),
                "description": "xAI Grok STT → Brain (GPT-4o-mini → Grok → Gemini) → Gemini 3.1 Flash TTS",
            },
            "pipeline_c": {
                "state":       "always_available",
                "description": "Text-only degraded mode",
            },
        }
