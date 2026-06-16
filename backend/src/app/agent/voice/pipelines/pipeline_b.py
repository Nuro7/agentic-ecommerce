"""
Pipeline B — xAI Grok Streaming STT → Brain (Gemini 2.5 Flash) → Gemini TTS  (FALLBACK)

Activates when Pipeline A (Gemini Live) circuit breaker opens.

Flow:
  Browser sends PCM Int16 16kHz mono chunks via WebSocket (binary)
    → forwarded in real-time to xAI Grok WebSocket STT (wss://api.x.ai/v1/stt)
    → xAI fires transcript.partial events:
        is_final=false              → interim transcript shown to user as they speak
        speech_final=true           → utterance complete → Brain → Gemini TTS → audio
    → Gemini 3.1 Flash TTS  → raw PCM bytes
    → sent as binary WebSocket frames (same wire format as Pipeline A)

Text input also supported:
  Browser sends {"type":"text_input","text":"..."}
    → Brain directly (no STT needed)
    → Gemini TTS → audio response

Keys required:
  GROK_API_KEY   — xAI Grok STT  (xai-...)
  GEMINI_API_KEY — Gemini TTS + Brain (same key)
  GEMINI_TTS_VOICE — TTS voice (default: Aoede)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from ....agent.orchestrator import AgentOrchestrator
from ....agent.prompts.filtering import detect_language
from ..gemini_tts import get_gemini_tts
from ..transcription import GrokStreamingSTT

logger = logging.getLogger(__name__)


class PipelineB:
    """
    xAI Grok WebSocket Streaming STT → Brain → Gemini TTS.
    Same Brain and TTS as before — only the STT layer changes.
    """

    def __init__(self, session_service: Any) -> None:
        self.session_service = session_service
        self._tts            = get_gemini_tts()
        self._orchestrators: dict[str, AgentOrchestrator] = {}

    def _get_orchestrator(self, session_id: str, store_client: Any) -> AgentOrchestrator:
        if session_id not in self._orchestrators:
            self._orchestrators[session_id] = AgentOrchestrator(
                store_client=store_client,
                session_service=self.session_service,
                tts_service=None,
            )
        return self._orchestrators[session_id]

    # ── One turn: Brain → TTS → send audio ───────────────────────────────────

    async def _process_turn(
        self,
        websocket: Any,
        session_id: str,
        text: str,
        language: str,
        store_client: Any = None,
        cart_context: Any = None,
    ) -> None:
        orchestrator = self._get_orchestrator(session_id, store_client)

        try:
            result = await orchestrator.run(
                session_id=session_id,
                user_message=text,
                language=language,
                cart_context=cart_context,
            )
            response_text = (
                result.get("speech_text")
                or result.get("text")
                or result.get("response_text")
                or ""
            )
            ui_actions  = result.get("ui_actions") or result.get("actions") or []
            suggestions = result.get("suggested_replies") or []
        except Exception as e:
            logger.error("Brain error session=%s: %s", session_id, e, exc_info=True)
            response_text = "Sorry, I had trouble with that. Could you try again?"
            ui_actions    = []
            suggestions   = []

        # Forward UI actions (show products, add-to-cart, etc.)
        for action in ui_actions:
            if action and action.get("type") not in (None, "noop"):
                try:
                    await websocket.send_text(json.dumps({"type": "ui_action", "action": action}))
                except Exception:
                    pass

        if suggestions:
            try:
                await websocket.send_text(json.dumps({"type": "suggestions", "items": suggestions}))
            except Exception:
                pass

        if response_text:
            try:
                await websocket.send_text(json.dumps({"type": "transcript", "text": response_text}))
            except Exception:
                pass

        # Gemini TTS → raw PCM → binary frame
        if response_text:
            try:
                pcm_bytes = await self._tts.synthesize(text=response_text, language=language)
                if pcm_bytes:
                    await websocket.send_bytes(pcm_bytes)
                    logger.info(
                        "Pipeline B TTS: lang=%s chars=%d pcm=%d bytes session=%s",
                        language, len(response_text), len(pcm_bytes), session_id,
                    )
            except Exception as e:
                logger.error("Gemini TTS error session=%s: %s", session_id, e, exc_info=True)

        try:
            await websocket.send_text(json.dumps({"type": "turn_complete"}))
        except Exception:
            pass

    # ── Main run loop ─────────────────────────────────────────────────────────

    async def run(self, websocket: Any, session_id: str, store_client: Any = None) -> None:
        """
        Run Pipeline B. Raises on unrecoverable error so the circuit breaker triggers.

        Two concurrent tasks:
          receive_and_forward — reads PCM from browser WS, streams to xAI STT
          process_events      — reads xAI transcript events, triggers Brain + TTS
        """
        api_key = os.environ.get("GROK_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("GROK_API_KEY not set — Pipeline B STT unavailable")

        try:
            await websocket.send_text(json.dumps({
                "type":     "pipeline_active",
                "pipeline": "B",
                "message":  "Using fallback voice mode.",
            }))
        except Exception:
            pass

        stt        = GrokStreamingSTT(api_key, language="en", endpointing=500)
        stop_event = asyncio.Event()

        # Connect to xAI Grok streaming STT
        await stt.connect()
        logger.info("Pipeline B: xAI Grok STT connected session=%s", session_id)

        # ── Task 1: browser PCM → xAI WS ─────────────────────────────────────

        async def receive_and_forward() -> None:
            try:
                while not stop_event.is_set():
                    data = await websocket.receive()

                    if data.get("type") == "websocket.disconnect":
                        break

                    # Binary PCM → forward directly to xAI (no WAV wrapping needed)
                    if "bytes" in data and data["bytes"]:
                        await stt.send_audio(data["bytes"])

                    # Text input → skip STT, go straight to Brain
                    elif "text" in data and data["text"]:
                        try:
                            ctrl = json.loads(data["text"])
                            if ctrl.get("type") == "text_input" and ctrl.get("text"):
                                user_text = ctrl["text"]
                                lang      = ctrl.get("language", "en")
                                cart_ctx  = ctrl.get("cart_context")
                                try:
                                    await websocket.send_text(json.dumps({
                                        "type": "user_transcript",
                                        "text": user_text,
                                    }))
                                except Exception:
                                    pass
                                await self._process_turn(
                                    websocket, session_id, user_text, lang,
                                    store_client=store_client,
                                    cart_context=cart_ctx,
                                )
                        except (json.JSONDecodeError, KeyError):
                            pass

            except Exception as e:
                logger.error("Frontend receive error session=%s: %s", session_id, e)
            finally:
                stop_event.set()
                await stt.close()

        # ── Task 2: xAI transcript events → Brain → TTS ───────────────────────

        async def process_events() -> None:
            try:
                async for event in stt.events():
                    if stop_event.is_set():
                        break

                    evt_type = event.get("type")

                    if evt_type == "transcript.partial":
                        text         = (event.get("text") or "").strip()
                        is_final     = bool(event.get("is_final", False))
                        speech_final = bool(event.get("speech_final", False))

                        if not text:
                            continue

                        if not is_final:
                            # Interim — show live typing indicator to user
                            try:
                                await websocket.send_text(json.dumps({
                                    "type": "user_transcript_interim",
                                    "text": text,
                                }))
                            except Exception:
                                pass

                        elif speech_final:
                            # Utterance complete — detect language and process
                            language = _detect_lang(text)
                            logger.info(
                                "Pipeline B STT utterance: [%s] lang=%s session=%s",
                                text[:80], language, session_id,
                            )
                            try:
                                await websocket.send_text(json.dumps({
                                    "type": "user_transcript",
                                    "text": text,
                                }))
                            except Exception:
                                pass
                            await self._process_turn(
                                websocket, session_id, text, language,
                                store_client=store_client,
                            )
                        # is_final=true, speech_final=false → chunk-final, keep accumulating

                    elif evt_type == "error":
                        logger.warning(
                            "xAI STT error session=%s: %s",
                            session_id, event.get("message", "unknown"),
                        )

            except Exception as e:
                logger.error(
                    "Event process error session=%s: %s: %s",
                    session_id, type(e).__name__, e, exc_info=True,
                )
                raise
            finally:
                stop_event.set()

        # ── Full-duplex ───────────────────────────────────────────────────────

        try:
            fwd_task  = asyncio.create_task(receive_and_forward())
            evt_task  = asyncio.create_task(process_events())

            done, pending = await asyncio.wait(
                [fwd_task, evt_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

            # Re-raise any exception so the router's circuit breaker triggers.
            for task in done:
                if not task.cancelled():
                    exc = task.exception()
                    if exc is not None:
                        raise exc

        finally:
            self._orchestrators.pop(session_id, None)
            logger.info("Pipeline B closed: session=%s", session_id)


# ── Language detection helper ─────────────────────────────────────────────────

_LANG_MAP = {
    "english": "en", "malayalam": "ml", "tamil": "ta", "telugu": "te",
    "kannada": "kn",  "hindi": "hi",   "bengali": "bn", "gujarati": "gu",
    "punjabi": "pa",  "arabic": "ar",  "french": "fr",  "spanish": "es",
    "portuguese": "pt", "german": "de", "japanese": "ja", "korean": "ko",
}


def _detect_lang(text: str) -> str:
    """Best-effort language detection from transcript text. Defaults to 'en'."""
    try:
        lang = detect_language(text)
        return lang if lang else "en"
    except Exception:
        return "en"
