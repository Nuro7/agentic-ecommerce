"""
Pipeline A — Gemini 3.1 Flash Live + Brain  (PRIMARY, multilingual)

Architecture:
  Browser PCM 16kHz → Gemini Live (STT + language detect + interruption)
    → ONE tool: ask_brain(query, language)
        → Python Orchestrator  (product search, cart, orders, checkout)
        → returns: response_text + optional ui_action
    → Gemini TTS (natural voice, 70+ languages, barge-in aware)
  → Browser PCM 24kHz

Why ONE tool instead of many:
  - Gemini handles voice I/O only — all business logic stays in Python
  - Easier to test, debug, and swap the Brain independently
  - Language routing handled by orchestrator, not Gemini
  - Single surface for tool failures / retries
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from google.genai import types

from ....agent.gemini_client import (
    client,
    _GEMINI_LIVE_MODEL,
    inject_reconnect_context,
)
from ....agent.orchestrator import AgentOrchestrator

logger = logging.getLogger(__name__)

# ── System prompt for Pipeline A ─────────────────────────────────────────────
# Slimmer than the multi-tool prompt — Gemini only needs to know WHEN to call
# ask_brain, not the details of each operation. Brain handles the details.

def _build_system_prompt() -> str:
    store_name = "this store"
    currency   = os.environ.get("STORE_CURRENCY", "₹")
    shipping   = os.environ.get("STORE_SHIPPING_POLICY", "Standard shipping available.")
    returns    = os.environ.get("STORE_RETURNS_POLICY", "Returns accepted within 7 days.")
    payments   = os.environ.get("STORE_PAYMENT_METHODS", "UPI, Card, Cash on Delivery")

    return f"""You are Aria, the voice shopping assistant for {store_name}.

═══════════════════════════════════════════════════════
RULE 1 — YOU HAVE ONE JOB: VOICE INTERFACE
═══════════════════════════════════════════════════════
You are the voice layer. Your Brain handles all shopping logic.
For EVERY customer request — products, cart, orders, checkout, policies — call ask_brain immediately.
Do NOT try to answer from your own knowledge. ALWAYS call ask_brain first.
Speak the Brain's response naturally, as a voice assistant would.

═══════════════════════════════════════════════════════
RULE 2 — SCOPE
═══════════════════════════════════════════════════════
You ONLY assist with shopping at {store_name}.
For anything outside shopping: "I'm here to help you shop at {store_name}. What can I find for you?"
Never mention Gemini, Google, AI, or technology. You are the {store_name} assistant.

═══════════════════════════════════════════════════════
RULE 3 — LANGUAGE
═══════════════════════════════════════════════════════
Detect the customer's language from their speech.
Pass the detected language code (en/hi/ml/ta/te/kn/bn/gu/pa) to ask_brain.
Speak back in the same language the customer used.
Default to English if language is unclear.

Manglish signals (Malayalam in English script): njan, venam, undoo, ayyo, mathi,
cheyyamo, enthaanu, ningal, sheri, parayamo, kanikkamo, vangam, sheriyano
→ pass language="ml" to ask_brain when you hear these.

═══════════════════════════════════════════════════════
RULE 4 — WHEN TO CALL ask_brain
═══════════════════════════════════════════════════════
Call ask_brain for EVERYTHING shopping-related:
• Product queries: "show me shirts", "what do you have", "find X", any item name
• Cart: "my cart", "what did I add", "remove X"
• Orders: "my orders", "order status"
• Checkout: any address / payment / confirm intent
• Policies: shipping, delivery, returns, refund, payment methods
• Comparisons: "which is better", "difference between X and Y"
• Greetings that could be shopping: "hi", "hello" → call ask_brain

═══════════════════════════════════════════════════════
RULE 5 — VOICE RESPONSE FORMAT
═══════════════════════════════════════════════════════
Speak the Brain's response naturally. Short, conversational sentences.
No bullet lists, no markdown, no prices in symbols — say "four ninety-nine rupees" not "₹499".
Currency: {currency}
Store info (only when customer asks): Shipping: {shipping} | Returns: {returns} | Payments: {payments}
"""


# ── ask_brain tool declaration ────────────────────────────────────────────────

def _build_brain_tool() -> list[types.Tool]:
    """Single tool — Gemini calls this for every shopping intent."""
    return [
        types.Tool(function_declarations=[
            types.FunctionDeclaration(
                name="ask_brain",
                description=(
                    "Send the customer's request to the shopping brain. "
                    "Call this for EVERY shopping query: products, cart, orders, "
                    "checkout, policies, comparisons, greetings. "
                    "The brain accesses the live store catalog and handles all operations. "
                    "Never answer product or price questions from your own knowledge."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "query": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "The customer's exact request in their language. "
                                "Preserve the original phrasing — do not translate or paraphrase."
                            ),
                        ),
                        "language": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Detected language code: en, hi, ml, ta, te, kn, bn, gu, pa. "
                                "Detect from the customer's speech. Use 'ml' for Manglish. "
                                "Default: en."
                            ),
                        ),
                    },
                    required=["query"],
                ),
            )
        ])
    ]


# ── Pipeline A handler ────────────────────────────────────────────────────────

class PipelineA:
    """
    Gemini Live + Brain pipeline.

    Gemini owns:  STT, language detection, TTS, interruption/barge-in
    Brain owns:   product search, cart, orders, checkout, all business logic
    """

    def __init__(self, session_service: Any) -> None:
        self.session_service = session_service
        # Per-session orchestrators — keyed by session_id, cleaned up on disconnect
        self._orchestrators: dict[str, AgentOrchestrator] = {}

    def _get_orchestrator(self, session_id: str, store_client: Any) -> AgentOrchestrator:
        if session_id not in self._orchestrators:
            self._orchestrators[session_id] = AgentOrchestrator(
                store_client=store_client,
                session_service=self.session_service,
                tts_service=None,  # TTS is Gemini's responsibility in Pipeline A
            )
        return self._orchestrators[session_id]

    async def run(self, websocket: Any, session_id: str, store_client: Any) -> None:
        """
        Run Pipeline A for one WebSocket session.
        Raises on unrecoverable error so the router can trigger circuit breaker.
        """
        if client is None:
            raise RuntimeError("Gemini client not initialized — GEMINI_API_KEY missing")

        voice_name = os.environ.get("GEMINI_VOICE", "Aoede")

        # ── Session config ────────────────────────────────────────────────────
        # IMPORTANT: thinking_config MUST be set in the constructor — post-assignment
        # is silently ignored by the SDK serializer.
        # Leave language_code unset in SpeechConfig — Gemini auto-detects from speech.
        # Setting it locks the model to one language and breaks multilingual detection.
        live_config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=types.Content(
                parts=[types.Part.from_text(text=_build_system_prompt())]
            ),
            tools=_build_brain_tool(),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=voice_name
                    )
                )
            ),
            # Transcriptions: get text of both sides for widget display
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            thinking_config=types.ThinkingConfig(thinking_level="minimal"),
            history_config=types.HistoryConfig(
                initial_history_in_client_content=True
            ),
        )

        try:
            async with client.aio.live.connect(
                model=_GEMINI_LIVE_MODEL,
                config=live_config,
            ) as gemini_session:

                logger.info(f"Pipeline A open: session={session_id} voice={voice_name}")

                # Seed session history BEFORE starting dual tasks.
                # Must complete before audio relay or Gemini stays paused.
                await inject_reconnect_context(gemini_session, self.session_service, session_id)

                # Latest cart snapshot from the widget (sent on text_input frames),
                # so ask_brain reasons about the customer's REAL cart. Mutable holder
                # shared between Task A (writes) and Task B (reads).
                session_cart: dict = {"value": None}

                # ── Task A: Browser → Gemini ──────────────────────────────────
                async def receive_from_frontend() -> None:
                    chunks = 0
                    try:
                        while True:
                            data = await websocket.receive()

                            if data.get("type") == "websocket.disconnect":
                                logger.info(f"Frontend disconnect: session={session_id}")
                                break

                            # Binary: PCM Int16 16kHz mono from AudioWorklet
                            if "bytes" in data and data["bytes"]:
                                chunks += 1
                                if chunks == 1:
                                    logger.info(
                                        f"First audio chunk: {len(data['bytes'])}B "
                                        f"session={session_id}"
                                    )
                                # Per-chunk guard: a transient send failure (e.g. an
                                # audio frame arriving before the Live session is
                                # fully ready) must drop that frame, NOT tear down
                                # the whole session.
                                try:
                                    await gemini_session.send_realtime_input(
                                        audio=types.Blob(
                                            mime_type="audio/pcm;rate=16000",
                                            data=data["bytes"],
                                        )
                                    )
                                except Exception as send_exc:
                                    logger.debug(
                                        "Dropped audio chunk (send failed) session=%s: %s",
                                        session_id, send_exc,
                                    )

                            # Text: typed input or widget text message
                            elif "text" in data and data["text"]:
                                try:
                                    ctrl = json.loads(data["text"])
                                    if ctrl.get("type") == "text_input" and ctrl.get("text"):
                                        if ctrl.get("cart_context") is not None:
                                            session_cart["value"] = ctrl.get("cart_context")
                                        await gemini_session.send_realtime_input(
                                            text=ctrl["text"]
                                        )
                                except (json.JSONDecodeError, KeyError):
                                    pass

                    except Exception as e:
                        logger.error(f"Frontend receive error session={session_id}: {e}")

                # ── Task B: Gemini → Browser + Brain ─────────────────────────
                async def receive_from_gemini() -> None:
                    orchestrator = self._get_orchestrator(session_id, store_client)
                    resp_count = 0
                    try:
                        async for response in gemini_session.receive():
                            resp_count += 1

                            # ── Server content (audio + transcripts) ──────────
                            if response.server_content:
                                sc = response.server_content

                                # Barge-in: user spoke over AI → clear queued audio in widget
                                if getattr(sc, "interrupted", False):
                                    try:
                                        await websocket.send_text(
                                            json.dumps({"type": "flush_audio"})
                                        )
                                        logger.info(f"Barge-in flush session={session_id}")
                                    except Exception:
                                        pass

                                # What the customer said (in their language)
                                if getattr(sc, "input_transcription", None):
                                    user_text = getattr(sc.input_transcription, "text", "") or ""
                                    if user_text:
                                        logger.info(f"User: [{user_text[:100]}] session={session_id}")
                                        try:
                                            await websocket.send_text(json.dumps({
                                                "type": "user_transcript",
                                                "text": user_text,
                                            }))
                                        except Exception:
                                            pass

                                # What the assistant said
                                if getattr(sc, "output_transcription", None):
                                    assistant_text = getattr(sc.output_transcription, "text", "") or ""
                                    if assistant_text:
                                        logger.info(
                                            f"Assistant: [{assistant_text[:100]}] session={session_id}"
                                        )
                                        try:
                                            await websocket.send_text(json.dumps({
                                                "type": "transcript",
                                                "text": assistant_text,
                                            }))
                                        except Exception:
                                            pass

                                if sc.model_turn:
                                    for part in (sc.model_turn.parts or []):
                                        # Inline text (older SDK path)
                                        if part.text:
                                            try:
                                                await websocket.send_text(json.dumps({
                                                    "type": "transcript",
                                                    "text": part.text,
                                                }))
                                            except Exception:
                                                pass
                                        # PCM 24kHz audio — stream bytes directly to browser
                                        if part.inline_data and part.inline_data.data:
                                            await websocket.send_bytes(part.inline_data.data)

                                # Signal widget to finalise the streaming bubble
                                if getattr(sc, "turn_complete", False):
                                    try:
                                        await websocket.send_text(
                                            json.dumps({"type": "turn_complete"})
                                        )
                                    except Exception:
                                        pass

                            # ── Tool call: ask_brain ──────────────────────────
                            if response.tool_call:
                                function_responses = []
                                for fc in (response.tool_call.function_calls or []):
                                    call_id = fc.id or fc.name
                                    args = dict(fc.args) if fc.args else {}

                                    if fc.name == "ask_brain":
                                        query    = args.get("query", "")
                                        language = args.get("language", "en")
                                        logger.info(
                                            f"ask_brain: lang={language} "
                                            f"query=[{query[:80]}] session={session_id}"
                                        )
                                        try:
                                            result = await orchestrator.run(
                                                session_id=session_id,
                                                user_message=query,
                                                language=language,
                                                cart_context=session_cart["value"],
                                            )
                                            # Orchestrator returns a dict with:
                                            # text, response_text, ui_actions, actions, suggested_replies
                                            response_text = (
                                                result.get("speech_text")
                                                or result.get("text")
                                                or result.get("response_text")
                                                or ""
                                            )
                                            ui_actions = result.get("ui_actions") or result.get("actions") or []

                                            # Forward UI actions to widget (add-to-cart, show-products, etc.)
                                            for action in ui_actions:
                                                if action and action.get("type") not in (None, "noop"):
                                                    try:
                                                        await websocket.send_text(json.dumps({
                                                            "type": "ui_action",
                                                            "action": action,
                                                        }))
                                                    except Exception:
                                                        pass

                                            # Suggested replies for widget quick-taps
                                            suggestions = result.get("suggested_replies", [])
                                            if suggestions:
                                                try:
                                                    await websocket.send_text(json.dumps({
                                                        "type": "suggestions",
                                                        "items": suggestions,
                                                    }))
                                                except Exception:
                                                    pass

                                            logger.info(
                                                f"Brain response: [{response_text[:80]}] "
                                                f"actions={len(ui_actions)} session={session_id}"
                                            )
                                            function_responses.append(
                                                types.FunctionResponse(
                                                    name="ask_brain",
                                                    id=call_id,
                                                    response={"response": response_text},
                                                )
                                            )

                                        except Exception as exc:
                                            logger.error(
                                                f"Brain error session={session_id}: {exc}",
                                                exc_info=True,
                                            )
                                            function_responses.append(
                                                types.FunctionResponse(
                                                    name="ask_brain",
                                                    id=call_id,
                                                    response={
                                                        "response": (
                                                            "Sorry, I had trouble with that. "
                                                            "Could you try again?"
                                                        )
                                                    },
                                                )
                                            )

                                if function_responses:
                                    await gemini_session.send(
                                        input=types.LiveClientToolResponse(
                                            function_responses=function_responses
                                        )
                                    )

                        if resp_count == 0:
                            logger.warning(
                                f"Gemini returned 0 responses — check API key / quota "
                                f"session={session_id}"
                            )

                    except Exception as e:
                        logger.error(
                            f"Gemini receive error session={session_id}: "
                            f"{type(e).__name__}: {e}",
                            exc_info=True,
                        )
                        raise  # propagate so circuit breaker can record failure

                # ── Full-duplex relay ─────────────────────────────────────────
                frontend_task = asyncio.create_task(receive_from_frontend())
                gemini_task   = asyncio.create_task(receive_from_gemini())

                try:
                    done, _pending = await asyncio.wait(
                        [frontend_task, gemini_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    # Always cancel AND await both tasks so the loser actually
                    # unwinds (closes its socket, frees the loop) before we return
                    # — even when we re-raise below. Prevents zombie-task leaks on
                    # disconnect.
                    for task in (frontend_task, gemini_task):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(frontend_task, gemini_task, return_exceptions=True)

                # Re-raise any exception so the router's circuit breaker triggers.
                # asyncio.wait() does NOT auto-raise — we must check manually.
                for task in done:
                    if not task.cancelled():
                        exc = task.exception()
                        if exc is not None:
                            raise exc

        finally:
            # Clean up per-session orchestrator to release memory
            self._orchestrators.pop(session_id, None)
            logger.info(f"Pipeline A closed: session={session_id}")
