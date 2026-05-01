"""
services/tts_service_v2.py
TTS service with Redis cache. Google Cloud TTS is the primary provider
for all languages (Neural2 voices for Indian languages).

Redis cache: 5,000 entries, 24h TTL (vs old 300-entry in-memory dict).
Falls back to in-memory LRU when Redis is unavailable.

Drop-in replacement: same synthesize() signature as TTSService.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from collections import OrderedDict
from typing import Any, Optional

from agent.language import make_speech_friendly
from services.tts import TTSService   # re-use ElevenLabs / Groq / browser paths

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

TTS_CACHE_TTL = 86_400      # 24 h
TTS_CACHE_MAX = 5_000       # Redis key cap (enforced as LRU in memory fallback)
_MEM_CACHE_MAX = 500        # memory fallback size


class TTSServiceV2:
    """
    Extended TTS service with Redis cache and language-aware routing.
    """

    def __init__(self, redis_client=None):
        self._r = redis_client

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
            hit = await self._cache_get(cache_key)
            if hit is not None:
                logger.debug("TTS cache hit lang=%s", language)
                return hit

        audio_b64 = await self._route(speech_text, language)

        if audio_b64:
            await self._cache_set(cache_key, audio_b64)

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

    async def _cache_get(self, key: str) -> Optional[str]:
        # Try Redis first
        if self._r is not None:
            try:
                raw = await self._r.get(key)
                if raw:
                    return raw
            except Exception as e:
                logger.debug("TTS Redis GET failed: %s", e)
        # Memory fallback
        if key in self._mem:
            self._mem.move_to_end(key)
            return self._mem[key]
        return None

    async def _cache_set(self, key: str, value: str) -> None:
        # Redis
        if self._r is not None:
            try:
                await self._r.setex(key, TTS_CACHE_TTL, value)
                # Trim old keys lazily — just log; Redis handles eviction via maxmemory-policy
            except Exception as e:
                logger.debug("TTS Redis SET failed: %s", e)
        # Memory fallback (LRU eviction)
        self._mem[key] = value
        self._mem.move_to_end(key)
        if len(self._mem) > _MEM_CACHE_MAX:
            self._mem.popitem(last=False)


# ── Singleton ─────────────────────────────────────────────────────────────────

_instance: Optional[TTSServiceV2] = None


def get_tts_service_v2(redis_client=None) -> TTSServiceV2:
    global _instance
    if _instance is None:
        _instance = TTSServiceV2(redis_client=redis_client)
    elif redis_client is not None and _instance._r is None:
        _instance._r = redis_client
    return _instance
