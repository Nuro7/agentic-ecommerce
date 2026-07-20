from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, AsyncIterator

from google.genai import types

from ....config import settings
from ....agent.gemini_client import client, _GEMINI_LIVE_MODEL, inject_reconnect_context
from .base import BaseVoiceProvider

logger = logging.getLogger(__name__)


class GeminiLiveProvider(BaseVoiceProvider):
    """
    Voice provider wrapping the Gemini Live (A2A) implementation.
    """

    def __init__(self, session_service: Any) -> None:
        self.session_service = session_service
        self.session: Any = None
        self._exit_stack: Any = None

    async def connect(self, session_id: str, store_client: Any, tenant_id: str) -> None:
        if client is None:
            raise RuntimeError("Gemini client not initialized — GEMINI_API_KEY missing")

        voice_name = os.environ.get("GEMINI_VOICE", "Aoede")
        from ....modules.tenants.service import get_store_config_for_tenant
        from ..pipelines.pipeline_a import (
            build_live_config,
            _build_system_prompt,
            _build_brain_tool,
        )

        store_config = await get_store_config_for_tenant(tenant_id)

        live_config = build_live_config(
            settings,
            system_instruction=_build_system_prompt(store_config),
            voice_name=voice_name,
            tools=_build_brain_tool(),
        )

        # Use an AsyncExitStack to properly manage the async context manager of live connection
        from contextlib import AsyncExitStack
        self._exit_stack = AsyncExitStack()
        
        try:
            self.session = await self._exit_stack.enter_async_context(
                client.aio.live.connect(
                    model=_GEMINI_LIVE_MODEL,
                    config=live_config,
                )
            )
            logger.info("GeminiLiveProvider connected: session=%s voice=%s", session_id, voice_name)
            
            # Seed session history before starting
            await inject_reconnect_context(
                self.session, self.session_service, tenant_id, session_id
            )
        except Exception as e:
            await self.close()
            raise e

    async def send_audio_chunk(self, chunk: bytes) -> None:
        if self.session:
            await self.session.send_realtime_input(
                audio=types.Blob(
                    mime_type="audio/pcm;rate=16000",
                    data=chunk,
                )
            )

    async def send_text_input(self, text: str, language: str, cart_context: Any) -> None:
        # Bypassed in coordinator and routed directly to the Brain
        pass

    async def receive_events(self) -> AsyncIterator[dict]:
        if not self.session:
            raise RuntimeError("GeminiLiveProvider is not connected")

        async for response in self.session.receive():
            # ── Server content (audio + transcripts) ──────────
            if response.server_content:
                sc = response.server_content

                if getattr(sc, "interrupted", False):
                    yield {"type": "flush_audio"}

                if getattr(sc, "input_transcription", None):
                    user_text = getattr(sc.input_transcription, "text", "") or ""
                    if user_text:
                        yield {"type": "user_transcript", "text": user_text}

                if getattr(sc, "output_transcription", None):
                    assistant_text = getattr(sc.output_transcription, "text", "") or ""
                    if assistant_text:
                        yield {"type": "transcript", "text": assistant_text}

                if sc.model_turn:
                    for part in (sc.model_turn.parts or []):
                        if part.text:
                            yield {"type": "transcript", "text": part.text}
                        if part.inline_data and part.inline_data.data:
                            yield {"type": "audio", "data": part.inline_data.data}

                if getattr(sc, "turn_complete", False):
                    yield {"type": "turn_complete"}

            # ── Tool call: ask_brain ──────────────────────────
            if response.tool_call:
                for fc in (response.tool_call.function_calls or []):
                    call_id = fc.id or fc.name
                    args = dict(fc.args) if fc.args else {}
                    yield {
                        "type": "tool_call",
                        "call_id": call_id,
                        "name": fc.name,
                        "arguments": args,
                    }

    async def send_tool_response(self, call_id: str, name: str, response: str) -> None:
        if self.session:
            await self.session.send(
                input=types.LiveClientToolResponse(
                    function_responses=[
                        types.FunctionResponse(
                            name=name,
                            id=call_id,
                            response={"response": response},
                        )
                    ]
                )
            )

    async def close(self) -> None:
        if self._exit_stack:
            try:
                await self._exit_stack.aclose()
            except Exception as e:
                logger.debug("Error closing Gemini Live session context: %s", e)
            finally:
                self.session = None
                self._exit_stack = None
