from __future__ import annotations

import abc
from typing import Any, AsyncIterator


class BaseVoiceProvider(abc.ABC):
    """
    Abstract base class for all voice providers (Gemini Live, Split, OpenAI Realtime).
    Isolates provider-specific networking, connection, and event handling.
    """

    @abc.abstractmethod
    async def connect(self, session_id: str, store_client: Any, tenant_id: str) -> None:
        """Establish connection to the upstream voice service."""
        pass

    @abc.abstractmethod
    async def send_audio_chunk(self, chunk: bytes) -> None:
        """Send raw client PCM audio chunk to the upstream service."""
        pass

    @abc.abstractmethod
    async def send_text_input(self, text: str, language: str, cart_context: Any) -> None:
        """Send typed user text query to the voice session if supported, or raise."""
        pass

    @abc.abstractmethod
    def receive_events(self) -> AsyncIterator[dict]:
        """
        Yields unified events from the upstream service:
        {
            "type": "user_transcript" | "user_transcript_interim" | "transcript" | "audio" | "tool_call" | "turn_complete" | "flush_audio" | "error",
            "text": str,           # optional (for transcript types)
            "data": bytes,         # optional (for audio type)
            "call_id": str,        # optional (for tool_call type)
            "name": str,           # optional (for tool_call type)
            "arguments": dict,     # optional (for tool_call type)
            "message": str,        # optional (for error type)
        }
        """
        pass

    @abc.abstractmethod
    async def send_tool_response(self, call_id: str, name: str, response: str) -> None:
        """Send function tool execution output back to the voice model."""
        pass

    @abc.abstractmethod
    async def close(self) -> None:
        """Close connection and clean up resources."""
        pass
