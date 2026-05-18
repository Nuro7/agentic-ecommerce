from __future__ import annotations

import asyncio
import json
import logging
import os

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from google.genai import types

from ...agent.gemini_client import (
    client, _GEMINI_LIVE_MODEL, _WS_TOKEN_TTL,
    validate_ws_token, generate_ws_token,
    build_system_prompt, build_live_tools, inject_reconnect_context,
)
from ...agent.tools.base import execute_tool

logger = logging.getLogger(__name__)
router = APIRouter(tags=["voice"])

@router.get("/wooagent/ws-token")
async def get_ws_token(session_id: str = Query(..., min_length=4, max_length=128)):
    token = generate_ws_token(session_id)
    return {"token": token, "ttl": _WS_TOKEN_TTL}

@router.websocket("/wooagent/stream")
async def gemini_live_relay(websocket: WebSocket):
    """
    Gemini 3.1 Flash Live A2A relay.

    Browser → backend (binary):  raw PCM Int16 16kHz mono from AudioWorklet
    Browser → backend (text):    {"type":"text_input","text":"..."}

    Backend → browser (binary):  PCM 16-bit 24kHz mono from Gemini TTS
    Backend → browser (text):    {"type":"transcript","text":"..."}
                                 {"type":"ui_action","action":{...}}
                                 {"type":"flush_audio"}  — barge-in clear
    """
    session_id = websocket.query_params.get("session_id", "anonymous")
    token      = websocket.query_params.get("token", "")

    if not validate_ws_token(token, session_id):
        await websocket.close(code=4003, reason="Invalid or expired token")
        logger.warning(f"WebSocket rejected — bad token: session={session_id}")
        return

    await websocket.accept()

    if client is None:
        logger.error("Gemini client not initialized — check GEMINI_API_KEY")
        await websocket.close(code=1011, reason="Gemini client unavailable")
        return

    woo_client      = getattr(websocket.app.state, "store_client", None) or getattr(websocket.app.state, "woo_client", None)
    session_service = getattr(websocket.app.state, "session_service", None)

    # ── Connection-time diagnostics ───────────────────────────────────────────
    # These lines tell you immediately why tools might not work.
    store_url = getattr(getattr(woo_client, "wc", woo_client), "base_url", "") or ""
    if not store_url:
        logger.error(
            f"WOOCOMMERCE_STORE_URL is empty — all tool calls will return no data. "
            f"Set WOOCOMMERCE_STORE_URL in .env to your WordPress site URL. (session={session_id})"
        )
    else:
        logger.info(f"WooCommerce store: {store_url[:60]} (session={session_id})")

    if woo_client is None:
        logger.error(f"WooCommerce client is None — tool calls disabled (session={session_id})")

    tools_declared = len(build_live_tools()[0].function_declarations) if woo_client else 0
    logger.info(f"A2A stream connected: session={session_id} tools_declared={tools_declared}")

    try:
        # ── Session Config ────────────────────────────────────────────────────
        # IMPORTANT: thinking_config MUST be set inside the constructor.
        # Post-assignment (live_config.thinking_config = ...) is silently ignored
        # by the SDK serializer and the model receives no thinking config at all.
        #
        # history_config.initial_history_in_client_content=True:
        # Server pauses after setupComplete, waits for send_client_content calls,
        # then resumes realtime mode after turn_complete=True. We MUST always
        # send that signal (see _inject_reconnect_context) or the model never speaks.
        # ── Voice selection ───────────────────────────────────────────────────
        # MULTILINGUAL voices (support 70+ languages natively):
        #   Aoede, Charon, Fenrir, Kore, Leda, Orus, Sulafat, Zephyr
        # ENGLISH-ONLY voice (do NOT use for multilingual stores):
        #   Puck
        # Override via GEMINI_VOICE env var if you want a different voice.
        voice_name = os.environ.get("GEMINI_VOICE", "Aoede")

        live_config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=types.Content(
                parts=[types.Part.from_text(text=build_system_prompt())]
            ),
            tools=build_live_tools() if woo_client else [],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=voice_name
                    )
                )
                # Do NOT set language_code here — leave it unset so Gemini
                # auto-detects the customer's language from their speech.
                # Setting a fixed language_code locks the model to that language
                # and breaks multilingual detection.
            ),
            # Input transcription: get the customer's words as text in their language
            input_audio_transcription=types.AudioTranscriptionConfig(),
            # Output transcription: get the assistant's response text for display
            output_audio_transcription=types.AudioTranscriptionConfig(),
            thinking_config=types.ThinkingConfig(thinking_level="minimal"),
            history_config=types.HistoryConfig(
                initial_history_in_client_content=True
            ),
        )

        async with client.aio.live.connect(
            model=_GEMINI_LIVE_MODEL,
            config=live_config,
        ) as gemini_session:

            logger.info(f"Gemini Live session open: session={session_id}")

            # Inject session context BEFORE starting dual tasks.
            # This is synchronous (awaited) so audio relay only starts after
            # the history seeding phase is complete and the server is in
            # realtime mode. Concurrent injection caused a race where audio
            # arrived before the server unblocked.
            await inject_reconnect_context(gemini_session, session_service, session_id)

            # ── TASK A: Frontend → Gemini ──────────────────────────────────────
            # Audio:  send_realtime_input(audio=Blob(...))  — 16kHz PCM from AudioWorklet
            # Text:   send_realtime_input(text=...)         — text typed or TTS transcript
            #
            # DO NOT use send_client_content for text during active conversation.
            # send_client_content is reserved for history seeding (done above).
            async def receive_from_frontend():
                _chunks = 0
                try:
                    while True:
                        data = await websocket.receive()

                        if data.get("type") == "websocket.disconnect":
                            logger.info(f"Frontend disconnected (disconnect message): session={session_id}")
                            break

                        if "bytes" in data and data["bytes"]:
                            _chunks += 1
                            if _chunks == 1:
                                logger.info(f"First audio chunk: {len(data['bytes'])}B (session={session_id})")
                            # Audio PCM 16kHz → Gemini (STT handled natively)
                            await gemini_session.send_realtime_input(
                                audio=types.Blob(
                                    mime_type="audio/pcm;rate=16000",
                                    data=data["bytes"],
                                )
                            )

                        elif "text" in data and data["text"]:
                            try:
                                ctrl = json.loads(data["text"])
                                if ctrl.get("type") == "text_input" and ctrl.get("text"):
                                    # send_realtime_input for in-conversation text.
                                    # The SDK signals end-of-turn for text automatically.
                                    await gemini_session.send_realtime_input(
                                        text=ctrl["text"]
                                    )
                            except (json.JSONDecodeError, KeyError):
                                pass

                except WebSocketDisconnect:
                    logger.info(f"Frontend disconnected: session={session_id}")
                except Exception as e:
                    logger.error(f"Frontend receive error (session={session_id}): {e}")

            # ── TASK B: Gemini → Frontend ──────────────────────────────────────
            # A single ServerContent event can contain MULTIPLE parts at once:
            # e.g. audio chunk + transcript text in the same event.
            # Iterate ALL parts — never assume a single-part event.
            async def receive_from_gemini():
                _audio_sent  = 0
                _resp_count  = 0
                try:
                    async for response in gemini_session.receive():
                        _resp_count += 1

                        if response.server_content:
                            sc = response.server_content

                            # Barge-in: user spoke over the AI — tell browser to clear queued audio
                            if getattr(sc, "interrupted", False):
                                try:
                                    await websocket.send_text(json.dumps({"type": "flush_audio"}))
                                    logger.info(f"Barge-in: flush_audio → browser (session={session_id})")
                                except Exception:
                                    pass

                            # input_audio_transcription: what the CUSTOMER said, in their language.
                            # Arrives on sc.input_transcription (separate from model_turn).
                            if getattr(sc, "input_transcription", None):
                                user_text = getattr(sc.input_transcription, "text", "") or ""
                                if user_text:
                                    logger.info(f"User said [{user_text[:120]}] (session={session_id})")
                                    try:
                                        await websocket.send_text(json.dumps({
                                            "type": "user_transcript",
                                            "text": user_text,
                                        }))
                                    except Exception:
                                        pass

                            # output_audio_transcription: what the ASSISTANT said, in the detected language.
                            # Arrives on sc.output_transcription (separate from model_turn audio parts).
                            if getattr(sc, "output_transcription", None):
                                assistant_text = getattr(sc.output_transcription, "text", "") or ""
                                if assistant_text:
                                    logger.info(f"Assistant said [{assistant_text[:120]}] (session={session_id})")
                                    try:
                                        await websocket.send_text(json.dumps({
                                            "type": "transcript",
                                            "text": assistant_text,
                                        }))
                                    except Exception:
                                        pass

                            if sc.model_turn:
                                for part in sc.model_turn.parts:
                                    # Inline text parts (older SDK path — also forward as transcript)
                                    if part.text:
                                        try:
                                            await websocket.send_text(json.dumps({
                                                "type": "transcript",
                                                "text": part.text,
                                            }))
                                        except Exception:
                                            pass

                                    # Audio PCM 24kHz — stream bytes directly to browser
                                    if part.inline_data and part.inline_data.data:
                                        _audio_sent += 1
                                        if _audio_sent == 1:
                                            logger.info(
                                                f"First Gemini audio: {len(part.inline_data.data)}B"
                                                f" → browser (session={session_id})"
                                            )
                                        await websocket.send_bytes(part.inline_data.data)

                            # Signal the widget that this assistant turn is done so it
                            # can finalise the streaming bubble into one complete message.
                            if getattr(sc, "turn_complete", False):
                                try:
                                    await websocket.send_text(json.dumps({"type": "turn_complete"}))
                                except Exception:
                                    pass

                        # Tool calls — synchronous only (async not supported in 3.1 Flash Live)
                        if response.tool_call and woo_client:
                            function_responses = []
                            for fc in response.tool_call.function_calls:
                                tool_name = fc.name
                                # fc.args is already a dict in google-genai SDK
                                tool_args = dict(fc.args) if fc.args else {}
                                # fc.id may be None in v1alpha — use tool_name as fallback
                                # so Gemini can still correlate the response to the call.
                                call_id = fc.id or tool_name
                                logger.info(
                                    f"Tool call received: {tool_name} id={call_id} "
                                    f"args={tool_args} (session={session_id})"
                                )
                                try:
                                    # execute_tool already imported above
                                    tool_exec = await execute_tool(
                                        tool_name=tool_name,
                                        tool_args=tool_args,
                                        session_id=session_id,
                                        store_client=woo_client,
                                    )
                                    # Log result summary so failures are visible in logs
                                    result_ok = tool_exec.result.get("success", False)
                                    if not result_ok:
                                        logger.warning(
                                            f"Tool {tool_name} returned failure: "
                                            f"{tool_exec.result.get('error', 'no error field')} "
                                            f"(session={session_id})"
                                        )
                                    else:
                                        # Log a brief summary (e.g. product count)
                                        n = len(tool_exec.result.get("products", []))
                                        if n:
                                            logger.info(f"Tool {tool_name} → {n} products (session={session_id})")
                                        else:
                                            logger.info(f"Tool {tool_name} → ok (session={session_id})")

                                    if tool_exec.action and tool_exec.action.get("type") != "noop":
                                        try:
                                            await websocket.send_text(json.dumps({
                                                "type":   "ui_action",
                                                "action": tool_exec.action,
                                            }))
                                        except Exception:
                                            pass
                                    if tool_name in ("add_to_cart", "remove_from_cart", "update_cart_quantity"):
                                        cart_data = tool_exec.result.get("cart", {})
                                        if cart_data and session_service:
                                            try:
                                                await session_service.save_cart(session_id, cart_data)
                                            except Exception:
                                                pass
                                    function_responses.append(
                                        types.FunctionResponse(
                                            name=tool_name,
                                            id=call_id,
                                            response=tool_exec.result,
                                        )
                                    )
                                except Exception as tool_err:
                                    logger.error(
                                        f"Tool execution error {tool_name}: {tool_err} "
                                        f"(session={session_id})",
                                        exc_info=True,
                                    )
                                    function_responses.append(
                                        types.FunctionResponse(
                                            name=tool_name,
                                            id=call_id,
                                            response={"success": False, "error": str(tool_err)},
                                        )
                                    )
                            if function_responses:
                                await gemini_session.send(
                                    input=types.LiveClientToolResponse(
                                        function_responses=function_responses
                                    )
                                )

                    if _resp_count == 0:
                        logger.warning(
                            f"Gemini returned 0 responses — likely wrong api_version or quota issue"
                            f" (session={session_id})"
                        )
                    else:
                        logger.info(f"Gemini session closed after {_resp_count} responses (session={session_id})")

                except Exception as e:
                    logger.error(
                        f"Gemini receive error (session={session_id}): {type(e).__name__}: {e}",
                        exc_info=True,
                    )

            # ── Full-Duplex ────────────────────────────────────────────────────
            frontend_task = asyncio.create_task(receive_from_frontend())
            gemini_task   = asyncio.create_task(receive_from_gemini())

            done, pending = await asyncio.wait(
                [frontend_task, gemini_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

    except Exception as e:
        logger.error(f"A2A session error (session={session_id}): {type(e).__name__}: {e}", exc_info=True)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════