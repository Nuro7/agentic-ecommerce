"""
Pipeline Router — Active Pipeline Selector with Circuit Breaker + Health Monitor
Refactored to route turns through VoiceTurnCoordinator and provider abstraction.

Routing order (default):
  Pipeline A  →  Gemini Live Provider (model selection preserved)
                 Circuit breaker: opens after 3 failures, resets after 60s

  Pipeline B  →  Split Voice Provider (Grok STT + Gemini TTS preserved)
                 Circuit breaker: opens after 3 failures, resets after 120s

  Pipeline C  →  Text-only degraded mode
                 No circuit breaker (always available)
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
from ..providers.split_voice import SplitVoiceProvider
from ..providers.openai_realtime import OpenAIVoiceProvider
from ..coordinator import VoiceTurnCoordinator

# Shared Circuit Breaker implementation
from ....core.circuit_breaker import CircuitBreaker  # noqa: E402

logger = logging.getLogger(__name__)


class PipelineRouter:
    """
    Routes each voice WebSocket session through the selected provider or pipeline cascade:
      A (Gemini Live) → B (Split Voice) → C (Text fallback)

    Instantiate once at startup and store in app.state.pipeline_router.
    """

    def __init__(self, session_service: Any) -> None:
        self._session_service = session_service
        self._pipeline_c = PipelineC(session_service)

        self._breaker_a = CircuitBreaker(
            name="A", failure_threshold=3, recovery_timeout=60.0
        )
        self._breaker_b = CircuitBreaker(
            name="B", failure_threshold=3, recovery_timeout=120.0
        )

    # ── Routing ───────────────────────────────────────────────────────────────

    async def run(self, websocket: Any, session_id: str, store_client: Any = None,
                  voice_enabled: bool = True, tenant_id: str = DEV_TENANT_ID) -> None:
        """
        Route this session through the best available pipeline/provider.
        Supports explicit overrides via Settings.voice_provider, with fallback to cascading.
        """
        if not voice_enabled:
            logger.info("Text-only session (plan without voice): session=%s", session_id)
            await self._run_text_fallback(websocket, session_id, store_client, tenant_id)
            return

        provider_choice = (settings.voice_provider or "gemini_live").lower().strip()

        # ── 1. Explicit OpenAI Realtime Provider ──
        if provider_choice == "openai":
            logger.info("Using OpenAI Realtime provider: session=%s", session_id)
            try:
                provider = OpenAIVoiceProvider(self._session_service)
                coordinator = VoiceTurnCoordinator(websocket, provider, self._session_service)
                await coordinator.run(session_id, store_client, tenant_id)
                return
            except Exception as exc:
                logger.error("OpenAI Realtime provider failed session=%s: %s", session_id, exc, exc_info=True)
                await self._notify_fallback(
                    websocket, from_pipeline="OpenAI", to_pipeline="C",
                    message="OpenAI voice service interrupted. Switching to text mode.",
                )
                await self._run_text_fallback(websocket, session_id, store_client, tenant_id)
                return

        # ── 2. Explicit Split Voice Provider ──
        if provider_choice == "split":
            logger.info("Using Split Voice provider: session=%s", session_id)
            try:
                provider = SplitVoiceProvider(self._session_service)
                coordinator = VoiceTurnCoordinator(websocket, provider, self._session_service)
                await coordinator.run(session_id, store_client, tenant_id)
                return
            except Exception as exc:
                logger.error("Split Voice provider failed session=%s: %s", session_id, exc, exc_info=True)
                await self._notify_fallback(
                    websocket, from_pipeline="Split", to_pipeline="C",
                    message="Split voice service interrupted. Switching to text mode.",
                )
                await self._run_text_fallback(websocket, session_id, store_client, tenant_id)
                return

        # ── 3. Default Cascading: Gemini Live (A) -> Split (B) -> Text (C) ──
        
        # ── Pipeline A (Gemini Live) ──
        if self._breaker_a.is_available():
            try:
                provider = GeminiLiveProvider(self._session_service)
                coordinator = VoiceTurnCoordinator(websocket, provider, self._session_service)
                await coordinator.run(session_id, store_client, tenant_id)
                self._breaker_a.record_success()
                return
            except Exception as exc:
                self._breaker_a.record_failure()
                logger.error(
                    "Pipeline A failed session=%s: %s: %s | breaker=%s",
                    session_id, type(exc).__name__, exc, self._breaker_a.state, exc_info=True
                )
                await self._notify_fallback(
                    websocket, from_pipeline="A", to_pipeline="B",
                    message="Voice service interrupted. Reconnecting...",
                )

        # ── Pipeline B (Split STT/TTS) ──
        if self._breaker_b.is_available():
            try:
                provider = SplitVoiceProvider(self._session_service)
                coordinator = VoiceTurnCoordinator(websocket, provider, self._session_service)
                await coordinator.run(session_id, store_client, tenant_id)
                self._breaker_b.record_success()
                return
            except Exception as exc:
                self._breaker_b.record_failure()
                logger.error(
                    "Pipeline B failed session=%s: %s: %s | breaker=%s",
                    session_id, type(exc).__name__, exc, self._breaker_b.state, exc_info=True
                )
                await self._notify_fallback(
                    websocket, from_pipeline="B", to_pipeline="C",
                    message="Switching to text mode.",
                )

        # ── Pipeline C (Text Fallback) ──
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
        provider_choice = (settings.voice_provider or "gemini_live").lower().strip()
        if provider_choice == "openai":
            return "OpenAI Realtime"
        if provider_choice == "split":
            return "Split Voice"
        if self._breaker_a.is_available():
            return "A"
        if self._breaker_b.is_available():
            return "B"
        return "C"

    def health(self) -> dict:
        return {
            "active_pipeline": self.active_pipeline,
            "voice_provider": settings.voice_provider,
            "pipeline_a": {
                **self._breaker_a.health(),
                "description": "Gemini Live Voice via Turn Coordinator",
            },
            "pipeline_b": {
                **self._breaker_b.health(),
                "description": "Split Voice (Grok STT + Gemini TTS) via Turn Coordinator",
            },
            "pipeline_c": {
                "state":       "always_available",
                "description": "Text-only degraded mode",
            },
        }
