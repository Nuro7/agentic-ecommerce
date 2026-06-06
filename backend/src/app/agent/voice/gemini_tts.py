"""
Gemini 3.1 Flash TTS — text → raw PCM audio bytes.

Model:  gemini-3.1-flash-tts-preview
Key:    GEMINI_API_KEY  (same key used by Gemini Live and Brain)
Voice:  GEMINI_TTS_VOICE env var (default: Aoede — multilingual)

Returns raw 16-bit PCM 24kHz mono bytes — same format as Gemini Live output,
so Pipeline B audio can be sent directly as binary WebSocket frames to the browser.

Voice options (all multilingual):
  Aoede, Charon, Fenrir, Kore, Leda, Orus, Puck, Sulafat, Zephyr
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_GEMINI_TTS_MODEL = "gemini-3.1-flash-tts-preview"

# Per-language style notes fed into the director prompt
_LANG_STYLE: dict[str, str] = {
    "en": "Warm Indian-accented English. Conversational, helpful, upbeat.",
    "hi": "Natural Hindi. Warm, friendly, clear.",
    "ml": "Natural Malayalam. Warm, helpful, clear.",
    "ta": "Natural Tamil. Warm, helpful, clear.",
    "te": "Natural Telugu. Warm, helpful, clear.",
    "kn": "Natural Kannada. Warm, helpful, clear.",
    "bn": "Natural Bengali. Warm, helpful, clear.",
    "gu": "Natural Gujarati. Warm, helpful, clear.",
    "pa": "Natural Punjabi. Warm, helpful, clear.",
}


class GeminiTTSService:
    """
    Gemini 3.1 Flash TTS.
    Uses the same google-genai client and API key as the Brain and Gemini Live.
    Returns raw PCM bytes (16-bit, 24kHz, mono).
    """

    def __init__(self) -> None:
        self._client = None
        self._voice  = os.environ.get("GEMINI_TTS_VOICE", "Aoede")
        self._init_client()

    def _init_client(self) -> None:
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            logger.warning("GEMINI_API_KEY not set — GeminiTTSService disabled")
            return
        try:
            from google import genai
            self._client = genai.Client(api_key=api_key)
            logger.info(
                "GeminiTTSService ready — model=%s voice=%s",
                _GEMINI_TTS_MODEL, self._voice,
            )
        except Exception as e:
            logger.error("GeminiTTSService init failed: %s", e)

    @property
    def available(self) -> bool:
        return self._client is not None

    def _build_director_prompt(self, text: str, language: str) -> str:
        """Wrap Aria's response in a director prompt for expressive, natural TTS."""
        style = _LANG_STYLE.get(language, "Warm, conversational, helpful.")
        return (
            "# AUDIO PROFILE: Aria — AI Shopping Assistant\n"
            "## THE SCENE: Online store. Aria is helping a customer find and buy products.\n"
            "### DIRECTOR'S NOTES\n"
            f"Style: {style}\n"
            "Pacing: Natural conversational pace. Brief pause between sentences.\n\n"
            "#### TRANSCRIPT\n"
            f"{text}"
        )

    async def synthesize(
        self,
        text: str,
        language: str = "en",
        voice: Optional[str] = None,
    ) -> Optional[bytes]:
        """
        Convert text to raw PCM audio bytes using Gemini 3.1 Flash TTS.

        Args:
            text:     Response text to speak.
            language: Language code (en, hi, ml, ta, ...) — Gemini auto-adapts
                      voice to the language without needing a separate config.
            voice:    Override voice name. Falls back to GEMINI_TTS_VOICE env var.

        Returns:
            Raw 16-bit PCM 24kHz mono bytes, or None on failure.
        """
        if not self._client or not text or not text.strip():
            return None

        from google.genai import types

        voice_name = voice or self._voice

        try:
            prompt = self._build_director_prompt(text, language)
            response = await asyncio.to_thread(
                self._client.models.generate_content,
                model=_GEMINI_TTS_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                voice_name=voice_name
                            )
                        )
                    ),
                ),
            )

            # Extract raw PCM bytes from response
            parts = response.candidates[0].content.parts[0]
            raw_pcm: bytes = parts.inline_data.data

            if not raw_pcm:
                logger.warning(
                    "GeminiTTS returned empty audio for lang=%s text=[%s...]",
                    language, text[:40],
                )
                return None

            logger.info(
                "GeminiTTS synthesized: lang=%s voice=%s chars=%d bytes=%d",
                language, voice_name, len(text), len(raw_pcm),
            )
            return raw_pcm

        except (IndexError, AttributeError) as e:
            logger.error(
                "GeminiTTS response parse error lang=%s: %s", language, e
            )
            return None
        except Exception as e:
            logger.error(
                "GeminiTTS synthesis error lang=%s: %s", language, e,
                exc_info=True,
            )
            return None


# ── Singleton ─────────────────────────────────────────────────────────────────

_instance: Optional[GeminiTTSService] = None


def get_gemini_tts() -> GeminiTTSService:
    global _instance
    if _instance is None:
        _instance = GeminiTTSService()
    return _instance
