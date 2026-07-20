from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, AsyncIterator

from ....agent.prompts.filtering import detect_language
from ..transcription import GrokStreamingSTT
from ..gemini_tts import get_gemini_tts
from .base import BaseVoiceProvider

logger = logging.getLogger(__name__)


class SplitVoiceProvider(BaseVoiceProvider):
    """
    Split Voice Provider mapping Grok Streaming STT and Gemini TTS to the BaseVoiceProvider.
    """

    def __init__(self, session_service: Any) -> None:
        self.session_service = session_service
        self.stt: GrokStreamingSTT | None = None
        self.tts: Any = None
        self._event_queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self._stt_task: asyncio.Task | None = None
        self._last_language: str = "en"
        self._connected: bool = False

    async def connect(self, session_id: str, store_client: Any, tenant_id: str) -> None:
        api_key = os.environ.get("GROK_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("GROK_API_KEY not set — Split pipeline STT unavailable")

        self.stt = GrokStreamingSTT(api_key, language="en", endpointing=500)
        await self.stt.connect()
        self.tts = get_gemini_tts()

        self._connected = True
        self._stt_task = asyncio.create_task(self._process_stt_events())
        logger.info("SplitVoiceProvider connected: session=%s", session_id)

    async def send_audio_chunk(self, chunk: bytes) -> None:
        if self.stt and self._connected:
            await self.stt.send_audio(chunk)

    async def send_text_input(self, text: str, language: str, cart_context: Any) -> None:
        self._last_language = language or "en"
        # Mirror user transcript and queue a tool call
        await self._event_queue.put({"type": "user_transcript", "text": text})
        await self._event_queue.put({
            "type": "tool_call",
            "call_id": "text_turn",
            "name": "ask_brain",
            "arguments": {"query": text, "language": self._last_language},
        })

    async def _process_stt_events(self) -> None:
        try:
            if not self.stt:
                return
            async for event in self.stt.events():
                if not self._connected:
                    break

                evt_type = event.get("type")
                if evt_type == "transcript.partial":
                    text = (event.get("text") or "").strip()
                    is_final = bool(event.get("is_final", False))
                    speech_final = bool(event.get("speech_final", False))

                    if not text:
                        continue

                    if not is_final:
                        await self._event_queue.put({
                            "type": "user_transcript_interim",
                            "text": text,
                        })
                    elif speech_final:
                        # Detect language
                        try:
                            lang = detect_language(text) or "en"
                        except Exception:
                            lang = "en"
                        self._last_language = lang

                        await self._event_queue.put({"type": "user_transcript", "text": text})
                        await self._event_queue.put({
                            "type": "tool_call",
                            "call_id": "voice_turn",
                            "name": "ask_brain",
                            "arguments": {"query": text, "language": lang},
                        })
                elif evt_type == "error":
                    logger.warning("STT error from SplitVoiceProvider: %s", event.get("message"))
        except Exception as e:
            logger.error("Error processing SplitVoiceProvider STT events: %s", e, exc_info=True)
            await self._event_queue.put({"type": "error", "message": str(e)})

    async def receive_events(self) -> AsyncIterator[dict]:
        while self._connected:
            event = await self._event_queue.get()
            if event is None:
                break
            yield event

    async def send_tool_response(self, call_id: str, name: str, response: str) -> None:
        if not self._connected:
            return

        # 1. Yield final transcript
        await self._event_queue.put({"type": "transcript", "text": response})

        # 2. Synthesize audio
        if response and self.tts:
            try:
                # Split pipeline uses its own timeout (handled by coordinator)
                pcm_bytes = await self.tts.synthesize(text=response, language=self._last_language)
                if pcm_bytes:
                    await self._event_queue.put({"type": "audio", "data": pcm_bytes})
            except Exception as e:
                logger.error("TTS synthesis failed in SplitVoiceProvider: %s", e)

        # 3. Yield turn complete
        await self._event_queue.put({"type": "turn_complete"})

    async def close(self) -> None:
        self._connected = False
        if self._stt_task:
            self._stt_task.cancel()
            try:
                await self._stt_task
            except asyncio.CancelledError:
                pass
            self._stt_task = None

        if self.stt:
            await self.stt.close()
            self.stt = None

        await self._event_queue.put(None)
