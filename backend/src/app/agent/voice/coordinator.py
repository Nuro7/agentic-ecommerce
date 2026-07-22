from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from ...config import settings
from ...agent.orchestrator import AgentOrchestrator
from ...agent.guardrails import build_retrieved_context, validate_spoken_text
from .providers.base import BaseVoiceProvider

logger = logging.getLogger(__name__)


class VoiceTurnCoordinator:
    """
    Coordinates voice turn execution, state transitions, concurrency safety,
    timeout handling, microphone pause/resume logic, and structured logging.
    """

    def __init__(
        self,
        websocket: Any,
        provider: BaseVoiceProvider,
        session_service: Any,
    ) -> None:
        self.websocket = websocket
        self.provider = provider
        self.session_service = session_service
        self._orchestrators: dict[str, AgentOrchestrator] = {}

        # Concurrency protection
        self._ws_lock = asyncio.Lock()
        self._active_brain_task: asyncio.Task | None = None
        self._state = "IDLE"  # IDLE | LISTENING | THINKING | SPEAKING

        # Mic state (for pause/resume handling)
        self._mic_enabled = True

        # Grounding / verified truth for the current turn
        self.spoken_truth = {
            "names": set(),
            "full_names": set(),
            "prices": set(),
            "verified": "",
        }

        # Session metrics/IDs populated at connect
        self.session_id = ""
        self.tenant_id = ""
        self.store_client = None
        self.session_cart = {"value": None}
        self.page_context = {}

    def transition_state(self, new_state: str) -> None:
        """Transitions current state and emits structured logging."""
        old_state = self._state
        self._state = new_state
        logger.info(
            "State transition: session=%s old=%s new=%s",
            self.session_id, old_state, new_state,
            extra={
                "session_id": self.session_id,
                "tenant_id": self.tenant_id,
                "action": "state_transition",
                "old_state": old_state,
                "new_state": new_state,
            }
        )

    async def safe_send_text(self, payload: str) -> None:
        """Sends text to client websocket with serialization lock to prevent concurrency errors."""
        async with self._ws_lock:
            try:
                await self.websocket.send_text(payload)
            except Exception as e:
                logger.debug("Failed to send text payload session=%s: %s", self.session_id, e)

    async def safe_send_bytes(self, data: bytes) -> None:
        """Sends binary PCM payload to client websocket with serialization lock."""
        async with self._ws_lock:
            try:
                await self.websocket.send_bytes(data)
            except Exception as e:
                logger.debug("Failed to send binary payload session=%s: %s", self.session_id, e)

    def _get_orchestrator(self) -> AgentOrchestrator:
        if self.session_id not in self._orchestrators:
            self._orchestrators[self.session_id] = AgentOrchestrator(
                store_client=self.store_client,
                session_service=self.session_service,
                tts_service=None,
            )
        return self._orchestrators[self.session_id]

    async def run_brain_turn(
        self,
        query: str,
        language: str,
        call_id: str | None = None,
        name: str | None = None,
    ) -> None:
        """Spawns brain turn task, cancelling any currently running tasks to support barge-in."""
        if self._active_brain_task and not self._active_brain_task.done():
            logger.info("Cancelling active brain turn task session=%s", self.session_id)
            self._active_brain_task.cancel()
            try:
                await self._active_brain_task
            except asyncio.CancelledError:
                pass

        self._active_brain_task = asyncio.create_task(
            self._execute_brain_turn(query, language, call_id, name)
        )

    async def _execute_brain_turn(
        self,
        query: str,
        language: str,
        call_id: str | None = None,
        name: str | None = None,
    ) -> None:
        self.transition_state("THINKING")
        self._mic_enabled = False  # Pause mic while speaking/responding

        orchestrator = self._get_orchestrator()
        start_time = time.perf_counter()

        # Resolve store config for tenant context and page context
        store_context = None
        # page_context is set from client page_update messages (see _receive_from_frontend)
        page_context = self.page_context or {}
        try:
            from ...modules.tenants.service import get_store_config_for_tenant
            store_config = await get_store_config_for_tenant(self.tenant_id)
            if store_config:
                store_context = {
                    "store_name": store_config.get("store_name") or "this store",
                    "currency_symbol": store_config.get("currency_symbol") or "₹",
                    "tenant_id": self.tenant_id,
                    "url": store_config.get("store_url") or "",
                }
        except Exception as e:
            logger.warning("Failed to resolve store config for session=%s: %s", self.session_id, e)

        logger.debug(
            "Brain turn: session=%s store_url=%s page_url=%s",
            self.session_id,
            (store_context or {}).get("url") or "<none>",
            page_context.get("url") or "<none>",
        )

        try:
            # Enforce execution timeout (15s)
            result = await asyncio.wait_for(
                orchestrator.run(
                    session_id=self.session_id,
                    user_message=query,
                    store_context=store_context,
                    page_context=page_context,
                    language=language,
                    cart_context=self.session_cart["value"],
                    tenant_id=self.tenant_id,
                ),
                timeout=15.0,
            )

            latency = (time.perf_counter() - start_time) * 1000.0
            logger.info(
                "Brain execution completed: session=%s query_len=%d latency_ms=%.2f",
                self.session_id, len(query), latency,
                extra={
                    "session_id": self.session_id,
                    "tenant_id": self.tenant_id,
                    "action": "brain_turn",
                    "latency_ms": latency,
                    "query_length": len(query),
                }
            )

            response_text = (
                result.get("speech_text")
                or result.get("text")
                or result.get("response_text")
                or ""
            )
            ui_actions = result.get("ui_actions") or result.get("actions") or []

            # Capture brain's verified grounding context
            try:
                _ids, _pr, _at, _nm, _full = build_retrieved_context(
                    [a.get("payload", {}) for a in ui_actions if isinstance(a, dict)]
                )
                self.spoken_truth.update(
                    names=_nm, full_names=_full, prices=_pr, verified=response_text
                )
            except Exception:
                pass

            # Forward UI actions to client
            for action in ui_actions:
                if action and action.get("type") not in (None, "noop"):
                    await self.safe_send_text(json.dumps({"type": "ui_action", "action": action}))

            # Suggested replies
            suggestions = result.get("suggested_replies") or []
            if suggestions:
                await self.safe_send_text(json.dumps({"type": "suggestions", "items": suggestions}))

            # Return response text to the provider context
            if call_id and name:
                await self.provider.send_tool_response(call_id, name, response_text)
            else:
                # Text-only text turn
                if response_text:
                    await self.safe_send_text(json.dumps({"type": "transcript", "text": response_text}))
                
                await self.safe_send_text(json.dumps({"type": "turn_complete"}))

        except asyncio.TimeoutError:
            logger.error(
                "Brain execution timeout: session=%s timeout=15s",
                self.session_id,
                extra={"session_id": self.session_id, "action": "brain_timeout"},
            )
            error_msg = "Sorry, I had trouble with that. Could you try again?"
            if call_id and name:
                await self.provider.send_tool_response(call_id, name, error_msg)
            else:
                await self.safe_send_text(json.dumps({"type": "transcript", "text": error_msg}))
                await self.safe_send_text(json.dumps({"type": "turn_complete"}))

        except Exception as e:
            logger.error("Brain execution error session=%s: %s", self.session_id, e, exc_info=True)
            error_msg = "Sorry, I had trouble with that. Could you try again?"
            if call_id and name:
                await self.provider.send_tool_response(call_id, name, error_msg)
            else:
                await self.safe_send_text(json.dumps({"type": "transcript", "text": error_msg}))
                await self.safe_send_text(json.dumps({"type": "turn_complete"}))

        finally:
            self.transition_state("IDLE")
            self._mic_enabled = True

    async def handle_text_input(self, text: str, language: str, cart_context: Any) -> None:
        """Handles explicit client-side text input queries by routing straight to the Brain."""
        if cart_context is not None:
            self.session_cart["value"] = cart_context

        logger.info("Coordinator text_input: session=%s query=[%s...]", self.session_id, text[:40])
        await self.safe_send_text(json.dumps({"type": "user_transcript", "text": text}))
        await self.run_brain_turn(text, language)

    async def run(self, session_id: str, store_client: Any, tenant_id: str) -> None:
        """Runs the turn coordinator event loop for a WebSocket session."""
        self.session_id = session_id
        self.store_client = store_client
        self.tenant_id = tenant_id

        await self.provider.connect(session_id, store_client, tenant_id)
        self.transition_state("LISTENING")

        recv_frontend_task = asyncio.create_task(self._receive_from_frontend())
        recv_provider_task = asyncio.create_task(self._receive_from_provider())

        try:
            done, pending = await asyncio.wait(
                [recv_frontend_task, recv_provider_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            await asyncio.gather(recv_frontend_task, recv_provider_task, return_exceptions=True)

            # Check and raise any exception
            for t in done:
                if not t.cancelled():
                    exc = t.exception()
                    if exc is not None:
                        raise exc
        finally:
            if self._active_brain_task and not self._active_brain_task.done():
                self._active_brain_task.cancel()
            self._orchestrators.pop(session_id, None)
            await self.provider.close()
            logger.info("VoiceTurnCoordinator closed: session=%s", session_id)

    async def _receive_from_frontend(self) -> None:
        try:
            while True:
                data = await self.websocket.receive()
                if data.get("type") == "websocket.disconnect":
                    logger.info("Frontend disconnected: session=%s", self.session_id)
                    break

                # Raw binary PCM Int16 from client media stream
                if "bytes" in data and data["bytes"]:
                    if self._mic_enabled:
                        await self.provider.send_audio_chunk(data["bytes"])
                    else:
                        # Drop audio frames when bot is speaking to prevent echoes / loopbacks
                        pass

                # Control messages
                elif "text" in data and data["text"]:
                    try:
                        ctrl = json.loads(data["text"])
                        if ctrl.get("type") == "text_input" and ctrl.get("text"):
                            await self.handle_text_input(
                                ctrl["text"],
                                ctrl.get("language", "en"),
                                ctrl.get("cart_context"),
                            )
                        elif ctrl.get("type") == "page_update":
                            incoming = ctrl.get("page_context") or {}
                            self.page_context = incoming
                            if ctrl.get("cart_context") is not None:
                                self.session_cart["value"] = ctrl.get("cart_context")
                            interrupted_flow = incoming.get("interrupted_flow")
                            if interrupted_flow:
                                logger.info(
                                    "Session %s interrupted_flow detected: %s",
                                    self.session_id, interrupted_flow
                                )
                            else:
                                logger.info("Session %s page_context updated: %s", self.session_id, self.page_context)
                    except Exception as parse_exc:
                        logger.debug("Failed to parse client control frame session=%s: %s", self.session_id, parse_exc)
        except Exception as e:
            logger.error("Error in receive_from_frontend loop session=%s: %s", self.session_id, e)
            raise

    async def _receive_from_provider(self) -> None:
        try:
            async for event in self.provider.receive_events():
                evt_type = event.get("type")

                if evt_type == "flush_audio":
                    if self._active_brain_task and not self._active_brain_task.done():
                        logger.info("Barge-in: cancelling active brain turn session=%s", self.session_id)
                        self._active_brain_task.cancel()
                    self.spoken_truth.update(names=set(), full_names=set(), prices=set(), verified="")
                    await self.safe_send_text(json.dumps({"type": "flush_audio"}))
                    self.transition_state("LISTENING")
                    self._mic_enabled = True

                elif evt_type == "user_transcript_interim":
                    await self.safe_send_text(json.dumps({
                        "type": "user_transcript_interim",
                        "text": event.get("text"),
                    }))

                elif evt_type == "user_transcript":
                    await self.safe_send_text(json.dumps({
                        "type": "user_transcript",
                        "text": event.get("text"),
                    }))

                elif evt_type == "transcript":
                    text = event.get("text", "")
                    if text:
                        out_text = text
                        # Grounding checks against spoken truth to prevent product hallucinations
                        if self.spoken_truth["names"] or self.spoken_truth["full_names"]:
                            ok, _clean = validate_spoken_text(
                                text,
                                retrieved_names=self.spoken_truth["names"] or None,
                                retrieved_full_names=self.spoken_truth["full_names"] or None,
                                retrieved_prices=self.spoken_truth["prices"] or None,
                            )
                            if not ok:
                                out_text = self.spoken_truth["verified"] or text
                                logger.warning("Spoken transcript diverged from brain — substituting verified text session=%s", self.session_id)

                        await self.safe_send_text(json.dumps({
                            "type": "transcript",
                            "text": out_text,
                        }))

                elif evt_type == "audio":
                    await self.safe_send_bytes(event.get("data"))
                    self.transition_state("SPEAKING")
                    self._mic_enabled = False

                elif evt_type == "tool_call":
                    await self.run_brain_turn(
                        query=event.get("arguments", {}).get("query", ""),
                        language=event.get("arguments", {}).get("language", "en"),
                        call_id=event.get("call_id"),
                        name=event.get("name"),
                    )

                elif evt_type == "turn_complete":
                    self.spoken_truth.update(names=set(), full_names=set(), prices=set(), verified="")
                    await self.safe_send_text(json.dumps({"type": "turn_complete"}))
                    self.transition_state("LISTENING")
                    self._mic_enabled = True

                elif evt_type == "error":
                    raise RuntimeError(event.get("message", "Provider error"))
        except Exception as e:
            logger.error("Error in receive_from_provider loop session=%s: %s", self.session_id, e)
            raise
