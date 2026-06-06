"""Conversation audio logger — Phase 11.

Persists voice I/O to object storage for quality review and fine-tuning.
All uploads are fire-and-forget (asyncio.create_task) so they never block
the live voice pipeline.

Storage layout
--------------
  audio-logs/{tenant_id}/{YYYY-MM-DD}/{session_id}/{ts_ms}_input.wav
  audio-logs/{tenant_id}/{YYYY-MM-DD}/{session_id}/{ts_ms}_output.mp3

Controlled by AUDIO_LOGGING_ENABLED=true in .env (default: false).
When disabled or when object storage is not configured, all calls are no-ops.

Usage
-----
    logger = AudioLogger(storage=storage_client, enabled=settings.audio_logging_enabled)

    # fire-and-forget — returns immediately, upload happens in background
    audio_logger.log_input(tenant_id, session_id, pcm_bytes_or_wav_bytes)
    audio_logger.log_output(tenant_id, session_id, mp3_b64_str)
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from datetime import datetime, timezone
from typing import Optional, Union

from .object_storage import ObjectStorageClient

logger = logging.getLogger(__name__)


class AudioLogger:
    """Fire-and-forget audio uploader for conversation logs."""

    def __init__(
        self,
        storage: ObjectStorageClient,
        *,
        enabled: bool = False,
    ) -> None:
        self._storage = storage
        self._enabled = enabled and storage.enabled

        if self._enabled:
            logger.info("AudioLogger enabled — conversation audio will be persisted to object storage")
        else:
            logger.debug("AudioLogger disabled (enabled=%s storage.enabled=%s)", enabled, storage.enabled)

    # ── Public API (all fire-and-forget) ──────────────────────────────────────

    def log_input(
        self,
        tenant_id: str,
        session_id: str,
        audio: Union[bytes, str],
        *,
        fmt: str = "wav",
    ) -> None:
        """Log user voice input. Pass raw WAV bytes or base64-encoded string.

        Fire-and-forget — schedules background upload, returns immediately.
        """
        if not self._enabled:
            return
        try:
            data = _to_bytes(audio)
            key = _build_key(tenant_id, session_id, "input", fmt)
            asyncio.create_task(self._upload(key, data, content_type=_mime(fmt)))
        except Exception as exc:
            logger.debug("AudioLogger log_input scheduling failed: %s", exc)

    def log_output(
        self,
        tenant_id: str,
        session_id: str,
        audio: Union[bytes, str],
        *,
        fmt: str = "mp3",
    ) -> None:
        """Log Aria's TTS output. Pass raw MP3 bytes or base64-encoded string.

        Fire-and-forget — schedules background upload, returns immediately.
        """
        if not self._enabled:
            return
        try:
            data = _to_bytes(audio)
            key = _build_key(tenant_id, session_id, "output", fmt)
            asyncio.create_task(self._upload(key, data, content_type=_mime(fmt)))
        except Exception as exc:
            logger.debug("AudioLogger log_output scheduling failed: %s", exc)

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _upload(self, key: str, data: bytes, *, content_type: str) -> None:
        try:
            ok = await self._storage.upload(key, data, content_type=content_type)
            if ok:
                logger.debug("AudioLogger uploaded %s (%d bytes)", key, len(data))
        except Exception as exc:
            logger.debug("AudioLogger upload failed %s: %s", key, exc)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_key(tenant_id: str, session_id: str, direction: str, fmt: str) -> str:
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ts_ms = int(time.time() * 1000)
    # Sanitise IDs to safe path segments
    tid = _safe_id(tenant_id)
    sid = _safe_id(session_id)
    return f"audio-logs/{tid}/{date_str}/{sid}/{ts_ms}_{direction}.{fmt}"


def _safe_id(value: str) -> str:
    """Strip characters that are unsafe in S3 keys."""
    return "".join(c for c in (value or "unknown") if c.isalnum() or c in "-_.")[:64]


def _to_bytes(audio: Union[bytes, str]) -> bytes:
    """Accept raw bytes or base64-encoded string, return bytes."""
    if isinstance(audio, bytes):
        return audio
    try:
        return base64.b64decode(audio)
    except Exception:
        return audio.encode("utf-8", errors="replace")


def _mime(fmt: str) -> str:
    return {"mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg"}.get(fmt, "audio/octet-stream")


# ── No-op singleton used when storage is not configured ──────────────────────

_noop: Optional[AudioLogger] = None


def get_noop_audio_logger() -> "AudioLogger":
    global _noop
    if _noop is None:
        from .object_storage import _NoOpStorageClient
        _noop = AudioLogger(_NoOpStorageClient(), enabled=False)
    return _noop
