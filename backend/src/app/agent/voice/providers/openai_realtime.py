from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import struct
import time
from enum import Enum
from typing import Any, AsyncIterator

import websockets

from ....config import settings
from .base import BaseVoiceProvider

logger = logging.getLogger(__name__)


class ResponseState(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    WAITING_FOR_TOOL = "waiting_for_tool"
    TOOL_RUNNING = "tool_running"
    RESPONSE_CREATED = "response_created"
    STREAMING_AUDIO = "streaming_audio"
    COMPLETED = "completed"
    CANCELLING = "cancelling"


def resample_pcm16_16k_to_24k(data: bytes) -> bytes:
    """
    Resample 16kHz PCM16 mono audio to 24kHz PCM16 mono using linear interpolation.
    Required because browser sends 16kHz, but OpenAI Realtime API requires exactly 24kHz.
    """
    num_samples = len(data) // 2
    if num_samples == 0:
        return b""
    samples = struct.unpack(f"<{num_samples}h", data)
    target_len = int(num_samples * 1.5)
    out_samples = [0] * target_len
    for i in range(target_len):
        src_idx = i / 1.5
        idx = int(src_idx)
        frac = src_idx - idx
        if idx < num_samples - 1:
            val = int(samples[idx] * (1.0 - frac) + samples[idx + 1] * frac)
        else:
            val = samples[idx]
        out_samples[i] = max(-32768, min(32767, val))
    return struct.pack(f"<{target_len}h", *out_samples)


class OpenAIVoiceProvider(BaseVoiceProvider):
    """
    Voice provider wrapping the OpenAI Realtime API using gpt-realtime-2.1 (GA).
    Complies with the official OpenAI Realtime GA specification:
    - Connects to wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1
    - Uses semantic_vad for turn detection
    - Uses output_modalities at session level only
    """

    def __init__(self, session_service: Any) -> None:
        self.session_service = session_service
        self.ws: websockets.WebSocketClientProtocol | None = None
        self._connected = False
        self.write_lock = asyncio.Lock()
        self._response_state = ResponseState.IDLE
        self.event_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None
        
        # Diagnostics & History
        self._inbound_history: list[str] = []
        self._outbound_history: list[str] = []
        self._response_created_received = False
        self._audio_delta_received = False
        self._response_done_received = False

    def _transition_state(self, new_state: ResponseState) -> None:
        """Logs and performs response state transitions."""
        old_state = self._response_state
        self._response_state = new_state
        logger.info("STATE [%s] -> [%s]", old_state.name, new_state.name)

    async def _send_safe(self, data: str) -> None:
        """
        Thread-safe websocket send operation guarded by an asyncio Lock.
        Logs every outbound event with detailed identifiers.
        """
        if self.ws and self._connected:
            try:
                try:
                    payload = json.loads(data)
                    evt_type = payload.get("type")
                    event_id = payload.get("event_id", "")
                    resp_id = payload.get("response", {}).get("id") or payload.get("response_id", "")
                    call_id = payload.get("call_id", "")
                    item_id = payload.get("item", {}).get("id") or payload.get("item_id", "")
                    
                    logger.info(
                        ">>> SEND [%s] id=%s resp_id=%s call_id=%s item_id=%s payload=%s",
                        evt_type, event_id, resp_id, call_id, item_id, data
                    )
                    
                    if evt_type:
                        self._outbound_history.append(evt_type)
                        if len(self._outbound_history) > 20:
                            self._outbound_history.pop(0)
                except Exception:
                    pass
                async with self.write_lock:
                    await self.ws.send(data)
            except Exception as e:
                logger.error("Error during thread-safe ws.send: %s", e)
                raise

    async def _ws_reader_loop(self) -> None:
        """
        The single dedicated coroutine responsible for reading raw WebSocket events
        from OpenAI, parsing them, and placing them in self.event_queue.
        """
        try:
            async for raw in self.ws:
                if not self._connected:
                    break
                try:
                    event = json.loads(raw)
                except Exception:
                    continue

                evt_type = event.get("type")
                event_id = event.get("event_id", "")
                resp_id = event.get("response", {}).get("id") or event.get("response_id", "")
                call_id = event.get("call_id", "")
                item_id = event.get("item", {}).get("id") or event.get("item_id", "")
                
                logger.info(
                    "<<< RECV [%s] id=%s resp_id=%s call_id=%s item_id=%s payload=%s",
                    evt_type, event_id, resp_id, call_id, item_id, raw
                )

                if evt_type:
                    self._inbound_history.append(evt_type)
                    if len(self._inbound_history) > 20:
                        self._inbound_history.pop(0)

                # Track active response state directly inside the reader
                if evt_type == "input_audio_buffer.speech_started":
                    self._transition_state(ResponseState.TRANSCRIBING)
                elif evt_type == "conversation.item.created":
                    item = event.get("item", {})
                    if item.get("type") == "function_call_output":
                        self._transition_state(ResponseState.TOOL_RUNNING)
                elif evt_type == "response.created":
                    self._transition_state(ResponseState.RESPONSE_CREATED)
                    self._response_created_received = True
                    self._audio_delta_received = False
                    self._response_done_received = False
                elif evt_type in ("response.output_audio.delta", "response.output_audio_transcript.delta"):
                    self._transition_state(ResponseState.STREAMING_AUDIO)
                    self._audio_delta_received = True
                elif evt_type == "response.function_call_arguments.done":
                    self._transition_state(ResponseState.WAITING_FOR_TOOL)
                elif evt_type == "response.done":
                    self._transition_state(ResponseState.COMPLETED)
                    self._response_done_received = True
                elif evt_type == "error":
                    err = event.get("error", {})
                    err_code = err.get("code")
                    err_msg = err.get("message", "")
                    logger.error("OpenAI Error Event: %s", json.dumps(event, indent=2))
                    if not (err_code in ("cancellation_failed", "response_cancel_not_active") or "cancellation failed" in err_msg.lower()):
                        # Reset to LISTENING for recoverable conversation interruptions or errors
                        self._transition_state(ResponseState.LISTENING)
                        self._response_created_received = False
                        self._audio_delta_received = False
                        self._response_done_received = False

                # Put the event in the queue
                await self.event_queue.put(event)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Error in OpenAI Realtime ws reader loop: %s", e)
        finally:
            self._connected = False
            self._transition_state(ResponseState.IDLE)
            self._response_created_received = False
            self._audio_delta_received = False
            self._response_done_received = False

    async def connect(self, session_id: str, store_client: Any, tenant_id: str) -> None:
        self._response_state = ResponseState.IDLE
        self._response_created_received = False
        self._audio_delta_received = False
        self._response_done_received = False
        self._inbound_history = []
        self._outbound_history = []
        
        api_key = settings.openai_api_key or os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set — OpenAI provider unavailable")

        model_name = settings.openai_realtime_model or "gpt-realtime-2.1"
        url = f"wss://api.openai.com/v1/realtime?model={model_name}"
        headers = {
            "Authorization": f"Bearer {api_key}",
        }

        # Handle modern websockets connection settings (heartbeats and custom headers)
        import inspect
        connect_kwargs = {
            "ping_interval": 20,
            "ping_timeout": 20,
        }
        sig = inspect.signature(websockets.connect)
        if "additional_headers" in sig.parameters:
            connect_kwargs["additional_headers"] = headers
        else:
            connect_kwargs["extra_headers"] = headers

        logger.info("Opening OpenAI Realtime WebSocket connection for session=%s", session_id)
        # Establish connection with timeout guard
        self.ws = await asyncio.wait_for(
            websockets.connect(url, **connect_kwargs),
            timeout=10.0
        )
        self._connected = True
        logger.info("OpenAIVoiceProvider connected to model=%s", model_name)

        # Start the reader task immediately (the ONLY WebSocket reader)
        self.event_queue = asyncio.Queue()
        self._reader_task = asyncio.create_task(self._ws_reader_loop())

        # Build session config
        from ....modules.tenants.service import get_store_config_for_tenant
        from ..pipelines.pipeline_a import _build_system_prompt

        store_config = await get_store_config_for_tenant(tenant_id)
        system_instruction = _build_system_prompt(store_config)

        openai_tools = [
            {
                "type": "function",
                "name": "ask_brain",
                "description": (
                    "Send the customer's request to the shopping brain. "
                    "Call this for EVERY shopping query: products, cart, orders, "
                    "checkout, policies, comparisons, greetings. "
                    "The brain accesses the live store catalog and handles all operations. "
                    "Never answer product or price questions from your own knowledge."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The customer's exact request in their language. Preserve the original phrasing."
                        },
                        "language": {
                            "type": "string",
                            "description": "Detected language code: en, hi, ml, ta, te, kn, bn, gu, pa. Use 'ml' for Manglish."
                        }
                    },
                    "required": ["query"]
                }
            }
        ]

        logger.info("Sending GA session.update config for session=%s", session_id)
        # Send GA configuration payload
        await self._send_safe(json.dumps({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "output_modalities": ["audio"],
                "instructions": system_instruction,
                "audio": {
                    "input": {
                        "format": {
                            "type": "audio/pcm",
                            "rate": 24000
                        },
                        "turn_detection": {
                            "type": "semantic_vad",   # GA spec: semantic_vad (not server_vad)
                            "create_response": True,  # Auto-generate response when user stops speaking
                            "interrupt_response": True # Auto-interrupt active response on new speech
                        },
                        "transcription": {
                            "model": "whisper-1"
                        }
                    },
                    "output": {
                        "format": {
                            "type": "audio/pcm",
                            "rate": 24000
                        },
                        "voice": settings.openai_realtime_voice or "alloy"
                    }
                },
                "tools": openai_tools,
                "tool_choice": "auto"
            }
        }))

        # Wait until session.updated is received directly from event_queue (read by background task)
        pre_handshake_events = []
        session_ready = False
        start_time = time.time()
        logger.info("Waiting for session.updated confirmation from event_queue...")
        while time.time() - start_time < 10.0:
            try:
                event = await asyncio.wait_for(self.event_queue.get(), timeout=1.0)
                evt_type = event.get("type")
                if evt_type == "session.updated":
                    logger.info("OpenAI Realtime session initialized successfully for session=%s", session_id)
                    self._transition_state(ResponseState.LISTENING)
                    session_ready = True
                    break
                elif evt_type == "error":
                    err = event.get("error", {})
                    logger.error("OpenAI Realtime session update failed: %s", err.get("message"))
                    raise RuntimeError(f"OpenAI session update failed: {err.get('message')}")
                else:
                    pre_handshake_events.append(event)
            except asyncio.TimeoutError:
                continue

        if not session_ready:
            raise RuntimeError("Timeout waiting for session.updated")

        # Now prepend pre-handshake events back to the event queue
        new_queue = asyncio.Queue()
        for ev in pre_handshake_events:
            await new_queue.put(ev)
        while not self.event_queue.empty():
            await new_queue.put(self.event_queue.get_nowait())
        self.event_queue = new_queue

    async def send_audio_chunk(self, chunk: bytes) -> None:
        if self.ws and self._connected:
            # Resample mono PCM from 16kHz to 24kHz
            resampled = resample_pcm16_16k_to_24k(chunk)
            payload = base64.b64encode(resampled).decode("utf-8")
            await self._send_safe(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": payload
            }))

    async def send_text_input(self, text: str, language: str, cart_context: Any) -> None:
        # Bypassed in coordinator and routed directly to the Brain
        pass

    async def cancel_response(self) -> None:
        """
        Manually request cancellation of the active response if in generating or streaming state.
        This is not triggered automatically by VAD because turn_detection's auto-interrupt takes care of VAD.
        """
        if self._response_state in (ResponseState.RESPONSE_CREATED, ResponseState.STREAMING_AUDIO):
            logger.info("Manual cancellation requested -> sending response.cancel")
            self._transition_state(ResponseState.CANCELLING)
            await self._send_safe(json.dumps({"type": "response.cancel"}))

    async def receive_events(self) -> AsyncIterator[dict]:
        while self._connected or not self.event_queue.empty():
            try:
                event = await asyncio.wait_for(self.event_queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue

            evt_type = event.get("type")

            if evt_type == "input_audio_buffer.speech_started":
                # Speech detected by server VAD -> barge-in!
                # Note: Server-side turn-detection auto-cancels the active response automatically.
                # Client only needs to transition state and flush current playback buffers.
                logger.info(
                    "Speech started event received. Diagnosis: "
                    "state=%s, response_created_received=%s, audio_delta_received=%s, response_done_received=%s, "
                    "inbound_history=%s, outbound_history=%s",
                    self._response_state.name,
                    self._response_created_received,
                    self._audio_delta_received,
                    self._response_done_received,
                    self._inbound_history,
                    self._outbound_history
                )
                yield {"type": "flush_audio"}

            elif evt_type == "conversation.item.input_audio_transcription.completed":
                transcript = event.get("transcript", "").strip()
                if transcript:
                    yield {"type": "user_transcript", "text": transcript}

            elif evt_type == "response.output_audio_transcript.delta":
                delta = event.get("delta")
                if delta:
                    yield {"type": "transcript", "text": delta}

            elif evt_type == "response.output_audio.delta":
                delta = event.get("delta")
                if delta:
                    audio_bytes = base64.b64decode(delta)
                    yield {"type": "audio", "data": audio_bytes}

            elif evt_type == "response.function_call_arguments.done":
                call_id = event.get("call_id")
                name = event.get("name")
                args_str = event.get("arguments", "{}")
                try:
                    arguments = json.loads(args_str)
                except Exception:
                    arguments = {}
                yield {
                    "type": "tool_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": arguments,
                }

            elif evt_type == "response.done":
                self._transition_state(ResponseState.LISTENING)
                yield {"type": "turn_complete"}

            elif evt_type == "error":
                err = event.get("error", {})
                err_msg = err.get("message", "Unknown OpenAI Realtime error")
                err_code = err.get("code")
                # Ignore non-fatal cancellation failure warnings
                if err_code in ("cancellation_failed", "response_cancel_not_active") or "cancellation failed" in err_msg.lower():
                    logger.warning("Ignored non-fatal OpenAI Realtime error: %s", err_msg)
                else:
                    logger.error("OpenAI Error: %s", json.dumps(event, indent=2))
                    yield {"type": "error", "message": err_msg}

    async def send_tool_response(self, call_id: str, name: str, response: str) -> None:
        if self.ws and self._connected:
            # Send function execution output
            await self._send_safe(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps({"response": response})
                }
            }))
            # Trigger response generation
            await self._send_safe(json.dumps({
                "type": "response.create"
            }))

    async def close(self) -> None:
        self._connected = False
        self._transition_state(ResponseState.IDLE)
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            self._reader_task = None
        if self.ws:
            try:
                await self.ws.close()
            except Exception as e:
                logger.warning("Error during websocket close: %s", e)
            self.ws = None
