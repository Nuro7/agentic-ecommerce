from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import struct
from typing import Any, AsyncIterator

import websockets

from ....config import settings
from .base import BaseVoiceProvider

logger = logging.getLogger(__name__)


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
    Voice provider wrapping the OpenAI Realtime API using GPT Realtime 2.1 Mini.
    """

    def __init__(self, session_service: Any) -> None:
        self.session_service = session_service
        self.ws: websockets.WebSocketClientProtocol | None = None
        self._connected = False

    async def connect(self, session_id: str, store_client: Any, tenant_id: str) -> None:
        api_key = settings.openai_api_key or os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set — OpenAI provider unavailable")

        model_name = settings.openai_realtime_model or "gpt-realtime-2.1-mini"
        url = f"wss://api.openai.com/v1/realtime?model={model_name}"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "OpenAI-Beta": "realtime=v1",
        }

        self.ws = await websockets.connect(url, extra_headers=headers)
        self._connected = True
        logger.info("OpenAIVoiceProvider connected to model=%s", model_name)

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

        await self.ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "instructions": system_instruction,
                "voice": settings.openai_realtime_voice or "alloy",
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "input_audio_transcription": {
                    "model": "whisper-1"
                },
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 600
                },
                "tools": openai_tools,
                "tool_choice": "auto"
            }
        }))

    async def send_audio_chunk(self, chunk: bytes) -> None:
        if self.ws and self._connected:
            # Resample mono PCM from 16kHz to 24kHz
            resampled = resample_pcm16_16k_to_24k(chunk)
            payload = base64.b64encode(resampled).decode("utf-8")
            await self.ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": payload
            }))

    async def send_text_input(self, text: str, language: str, cart_context: Any) -> None:
        # Bypassed in coordinator and routed directly to the Brain
        pass

    async def receive_events(self) -> AsyncIterator[dict]:
        if not self.ws:
            raise RuntimeError("OpenAIVoiceProvider is not connected")

        async for raw in self.ws:
            if not self._connected:
                break
            try:
                event = json.loads(raw)
            except Exception:
                continue

            evt_type = event.get("type")

            if evt_type == "input_audio_buffer.speech_started":
                # Speech detected by server VAD -> barge-in!
                # Send cancel command to OpenAI Realtime and yield flush_audio
                await self.ws.send(json.dumps({"type": "response.cancel"}))
                yield {"type": "flush_audio"}

            elif evt_type == "conversation.item.input_audio_transcription.completed":
                transcript = event.get("transcript", "").strip()
                if transcript:
                    yield {"type": "user_transcript", "text": transcript}

            elif evt_type == "response.audio_transcript.delta":
                delta = event.get("delta")
                if delta:
                    yield {"type": "transcript", "text": delta}

            elif evt_type == "response.audio.delta":
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
                yield {"type": "turn_complete"}

            elif evt_type == "error":
                err_msg = event.get("error", {}).get("message", "Unknown OpenAI Realtime error")
                logger.error("OpenAI Realtime error: %s", err_msg)
                yield {"type": "error", "message": err_msg}

    async def send_tool_response(self, call_id: str, name: str, response: str) -> None:
        if self.ws and self._connected:
            # Send function execution output
            await self.ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps({"response": response})
                }
            }))
            # Trigger response generation
            await self.ws.send(json.dumps({
                "type": "response.create"
            }))

    async def close(self) -> None:
        self._connected = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
