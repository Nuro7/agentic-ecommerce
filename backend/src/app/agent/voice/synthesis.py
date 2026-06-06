"""
services/tts_service_v2.py
TTS service with two-tier cache:
  L1  Redis  — hot cache, base64 strings, 24 h TTL, fast (~3 ms)
  L2  Object Storage (S3/R2/GCS) — warm cache, raw MP3 bytes, 30 d TTL

Phase 11 adds the L2 tier so large audio blobs are kept out of Redis memory.
Falls back to in-memory LRU when Redis is unavailable.

Cache read order:  Redis → Object Storage → Generate → store both
Cache write order: Object Storage first (durable), then Redis (fast lookup)

Drop-in replacement: same synthesize() signature as TTSService.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
from collections import OrderedDict
from typing import Any, Optional

from ..prompts.filtering import make_speech_friendly
from .tts_fallback import TTSService

logger = logging.getLogger(__name__)

# ── Google voice map ──────────────────────────────────────────────────────────
# Journey voices  → most natural conversational quality (en only, stereo 24kHz)
# Neural2 voices  → best neural quality for Indian languages
# Wavenet voices  → fallback for languages without Neural2

GOOGLE_VOICE_MAP: dict[str, tuple[str, str]] = {
    "en": ("en-IN", "en-IN-Journey-F"),    # Journey: designed for conversational AI
    "hi": ("hi-IN", "hi-IN-Neural2-A"),    # Neural2: female Hindi voice
    "ml": ("ml-IN", "ml-IN-Wavenet-A"),    # best available Malayalam
    "ta": ("ta-IN", "ta-IN-Wavenet-A"),
    "te": ("te-IN", "te-IN-Standard-A"),
    "kn": ("kn-IN", "kn-IN-Wavenet-A"),
    "bn": ("bn-IN", "bn-IN-Wavenet-A"),
    "gu": ("gu-IN", "gu-IN-Wavenet-A"),
    "pa": ("pa-IN", "pa-IN-Wavenet-B"),
}

# ── Cache constants ───────────────────────────────────────────────────────────

TTS_CACHE_TTL     = 86_400          # Redis TTL: 24 h
TTS_S3_CACHE_TTL  = 30 * 86_400    # S3 TTL: 30 days (informational — not enforced by S3 by default)
TTS_CACHE_MAX     = 5_000           # Redis key cap (enforced as LRU in memory fallback)
_MEM_CACHE_MAX    = 500             # memory fallback size
_S3_PREFIX        = "tts-cache"     # S3 key prefix for TTS audio blobs


class TTSServiceV2:
    """
    Extended TTS service with two-tier cache (Redis + Object Storage) and language-aware routing.
    """

    def __init__(self, redis_client=None, storage_client=None):
        self._r = redis_client
        self._storage = storage_client  # ObjectStorageClient or None

        self.provider       = os.getenv("TTS_PROVIDER", "google").lower()
        self.google_api_key = os.getenv("GOOGLE_TTS_API_KEY", "")

        # Memory LRU fallback (used when Redis is down)
        self._mem: OrderedDict[str, str] = OrderedDict()

        # Delegate elevenlabs / groq / browser to the original service
        self._fallback = TTSService()

        logger.info("TTSServiceV2: provider=%s, redis=%s", self.provider, "yes" if redis_client else "no")

    # ── Public API ────────────────────────────────────────────────────────────

    def audio_format(self) -> str:
        if self.provider in ("elevenlabs", "google"):
            return "mp3"
        return "wav"

    async def synthesize(
        self,
        text: str,
        language: str = "en",
        skip_cache: bool = False,
    ) -> Optional[str]:
        """
        Convert text → base64 audio. Returns None for browser TTS.
        make_speech_friendly() is applied automatically — don't call it first.
        """
        if not text or not text.strip():
            return None

        # Strip model thinking blocks
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        text = re.sub(r"<reasoning>.*?</reasoning>", "", text, flags=re.DOTALL).strip()
        if not text:
            return None

        speech_text = make_speech_friendly(text, language)
        if not speech_text:
            return None

        cache_key = self._cache_key(speech_text, language)

        if not skip_cache:
            hit = await self._cache_get(cache_key, language)
            if hit is not None:
                return hit

        audio_b64 = await self._route(speech_text, language)

        if audio_b64:
            await self._cache_set(cache_key, audio_b64, language)

        return audio_b64

    # ── Routing ───────────────────────────────────────────────────────────────

    async def _route(self, text: str, language: str) -> Optional[str]:
        if self.google_api_key:
            try:
                result = await self._google(text, language)
                if result:
                    return result
                logger.warning("Google TTS returned empty audio for lang=%s — trying fallback", language)
            except Exception as e:
                err = str(e)
                if "429" in err or "rate_limit" in err.lower():
                    logger.warning("TTS rate-limited (google, lang=%s) — trying fallback", language)
                elif "400" in err:
                    logger.error("TTS 400 Bad Request (google, lang=%s): %s — trying fallback", language, err[:200])
                else:
                    logger.error("TTS error (google, lang=%s): %s — trying fallback", language, e)
            # Google failed — try fallback provider
            try:
                return await self._fallback.synthesize(text, language, skip_cache=True)
            except Exception as e2:
                logger.warning("TTS fallback also failed (lang=%s): %s", language, e2)
                return None
        # No Google key — use fallback directly
        try:
            return await self._fallback.synthesize(text, language, skip_cache=True)
        except Exception as e:
            logger.warning("TTS fallback failed (lang=%s): %s", language, e)
            return None

    # ── Google Neural2 / Journey TTS ─────────────────────────────────────────

    def _to_ssml(self, text: str, lang_code: str) -> str:
        """
        Wrap plain text in SSML with natural pause breaks.
        - 350ms after sentence-ending punctuation (.  !  ?)
        - 150ms after commas and semicolons
        This makes the voice sound unhurried and conversational rather
        than reading text at a constant pace.
        """
        safe = (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;")
        )
        # Sentence breaks — natural breath pause
        safe = re.sub(r'([.!?])\s+', r'\1<break time="300ms"/> ', safe)
        # Clause breaks — short pause
        safe = re.sub(r'([,;])\s+', r'\1<break time="100ms"/> ', safe)
        # Ellipsis
        safe = re.sub(r'\.\.\.', r'<break time="400ms"/>', safe)
        # Journey voices sound most natural with NO prosody overrides —
        # artificial rate/pitch changes make them sound robotic.
        return f'<speak>{safe}</speak>'

    async def _google(self, text: str, language: str) -> Optional[str]:
        import httpx
        lang_code, voice_name = GOOGLE_VOICE_MAP.get(language, ("en-IN", "en-IN-Journey-F"))

        # Journey voices do NOT support SSML — plain text only.
        # Neural2/Wavenet voices support SSML for natural pause control.
        is_journey = "Journey" in voice_name
        if is_journey:
            tts_input = {"text": text}
        else:
            tts_input = {"ssml": self._to_ssml(text, lang_code)}

        payload = {
            "input": tts_input,
            "voice": {
                "languageCode": lang_code,
                "name": voice_name,
                "ssmlGender": "FEMALE",
            },
            "audioConfig": {
                "audioEncoding": "MP3",
                "speakingRate": 0.95,    # slightly slower than default — more conversational
                "pitch": 0.0,            # no pitch shift — natural voice
            },
        }
        async with httpx.AsyncClient(timeout=12.0) as client:
            r = await client.post(
                "https://texttospeech.googleapis.com/v1/text:synthesize",
                params={"key": self.google_api_key},
                json=payload,
            )
            if r.status_code != 200:
                logger.error("Google TTS %d: %s", r.status_code, r.text[:300])
            r.raise_for_status()
            return r.json().get("audioContent")   # already base64 MP3

    # ── Cache helpers ─────────────────────────────────────────────────────────

    def _cache_key(self, text: str, language: str) -> str:
        digest = hashlib.md5(f"{self.provider}:{text}:{language}".encode()).hexdigest()
        return f"tts:{digest}"

    def _s3_key(self, redis_key: str, language: str) -> str:
        """Derive a structured S3 key from the Redis cache key."""
        digest = redis_key.removeprefix("tts:")
        # Shard by first 2 chars to avoid flat namespace in S3
        return f"{_S3_PREFIX}/{language}/{digest[:2]}/{digest}.mp3"

    async def _cache_get(self, key: str, language: str = "en") -> Optional[str]:
        # L1: Redis (fast)
        if self._r is not None:
            try:
                raw = await self._r.get(key)
                if raw:
                    logger.debug("TTS L1 Redis hit lang=%s", language)
                    return raw
            except Exception as e:
                logger.debug("TTS Redis GET failed: %s", e)

        # L2: Object Storage (warm, only if L1 missed)
        if self._storage is not None and self._storage.enabled:
            try:
                s3_key = self._s3_key(key, language)
                data = await self._storage.download(s3_key)
                if data is not None:
                    b64 = base64.b64encode(data).decode()
                    # Backfill L1 Redis so the next hit is fast
                    await self._cache_set_redis_only(key, b64)
                    logger.debug("TTS L2 S3 hit lang=%s", language)
                    return b64
            except Exception as e:
                logger.debug("TTS S3 GET failed: %s", e)

        # L3: Memory fallback
        if key in self._mem:
            self._mem.move_to_end(key)
            return self._mem[key]

        return None

    async def _cache_set(self, key: str, value: str, language: str = "en") -> None:
        # L2: Object Storage first (durable, larger capacity)
        if self._storage is not None and self._storage.enabled:
            try:
                s3_key = self._s3_key(key, language)
                raw_bytes = base64.b64decode(value)
                await self._storage.upload(s3_key, raw_bytes, content_type="audio/mpeg")
                logger.debug("TTS L2 S3 stored lang=%s key=%s", language, s3_key)
            except Exception as e:
                logger.debug("TTS S3 SET failed: %s", e)

        # L1: Redis
        await self._cache_set_redis_only(key, value)

    async def _cache_set_redis_only(self, key: str, value: str) -> None:
        """Write only to Redis + memory (used for S3→Redis backfill)."""
        if self._r is not None:
            try:
                await self._r.setex(key, TTS_CACHE_TTL, value)
            except Exception as e:
                logger.debug("TTS Redis SET failed: %s", e)
        # Memory fallback (LRU eviction)
        self._mem[key] = value
        self._mem.move_to_end(key)
        if len(self._mem) > _MEM_CACHE_MAX:
            self._mem.popitem(last=False)


# ── Singleton ─────────────────────────────────────────────────────────────────

_instance: Optional[TTSServiceV2] = None


def get_tts_service_v2(redis_client=None, storage_client=None) -> TTSServiceV2:
    global _instance
    if _instance is None:
        _instance = TTSServiceV2(redis_client=redis_client, storage_client=storage_client)
    else:
        if redis_client is not None and _instance._r is None:
            _instance._r = redis_client
        if storage_client is not None and _instance._storage is None:
            _instance._storage = storage_client
    return _instance
