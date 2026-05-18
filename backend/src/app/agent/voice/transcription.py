from __future__ import annotations

import os
from typing import Tuple

import httpx


class STTService:
    def __init__(self) -> None:
        self.deepgram_api_key = os.getenv("DEEPGRAM_API_KEY", "").strip()
        self.groq_api_key = os.getenv("GROQ_API_KEY", "").strip()
        self.provider = self._resolve_provider(os.getenv("STT_PROVIDER", "groq").strip().lower())
        self.timeout = httpx.Timeout(25.0, connect=5.0)

    def _resolve_provider(self, requested: str) -> str:
        if requested == "deepgram" and self.deepgram_api_key:
            return "deepgram"
        if requested == "groq" and self.groq_api_key:
            return "groq"
        if self.groq_api_key:
            return "groq"
        if self.deepgram_api_key:
            return "deepgram"
        return "none"

    async def transcribe(self, audio_bytes: bytes, mime_type: str, language_hint: str = "") -> Tuple[str, float, str]:
        if os.getenv("MOCK_SERVICES", "false").lower() == "true":
            return "Mock: I would like to buy some shoes.", 0.95, "en"

        if self.provider == "groq":
            return await self._transcribe_groq(audio_bytes, mime_type, language_hint=language_hint)
        if self.provider == "deepgram":
            return await self._transcribe_deepgram(audio_bytes, mime_type, language_hint=language_hint)
        raise RuntimeError("No STT provider configured. Set GROQ_API_KEY or DEEPGRAM_API_KEY.")

    async def _transcribe_deepgram(self, audio_bytes: bytes, mime_type: str, language_hint: str = "") -> Tuple[str, float, str]:
        # If we have a language hint, pass it to Deepgram (disables auto-detect for faster, more accurate results)
        lang_param = f"&language={language_hint}" if language_hint else "&detect_language=true"
        url = (
            "https://api.deepgram.com/v1/listen"
            f"?model=nova-2&smart_format=true&punctuate=true{lang_param}"
        )

        headers = {
            "Authorization": f"Token {self.deepgram_api_key}",
            "Content-Type": mime_type or "audio/webm",
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(url, headers=headers, content=audio_bytes)
            response.raise_for_status()
            payload = response.json()

        channel = payload.get("results", {}).get("channels", [{}])[0]
        alternatives = channel.get("alternatives", [{}])
        best = alternatives[0] if alternatives else {}

        transcript = str(best.get("transcript", "")).strip()
        confidence = float(best.get("confidence", 0.0) or 0.0)
        language = str(best.get("detected_language") or channel.get("detected_language") or "unknown")
        if "-" in language:
            language = language.split("-")[0]

        return transcript, confidence, language

    async def _transcribe_groq(self, audio_bytes: bytes, mime_type: str, language_hint: str = "") -> Tuple[str, float, str]:
        model = os.getenv("GROQ_STT_MODEL", "whisper-large-v3-turbo").strip() or "whisper-large-v3-turbo"

        # Map 2-letter lang codes to Whisper's BCP-47 language codes
        # Whisper uses ISO 639-1 codes — these match what the frontend sends
        WHISPER_LANG_MAP = {
            "ml": "ml",   # Malayalam
            "ta": "ta",   # Tamil
            "te": "te",   # Telugu
            "kn": "kn",   # Kannada
            "hi": "hi",   # Hindi
            "bn": "bn",   # Bengali
            "gu": "gu",   # Gujarati
            "pa": "pa",   # Punjabi
            "en": "en",   # English
        }
        whisper_lang = WHISPER_LANG_MAP.get(language_hint, "") if language_hint else ""

        files = {
            "file": ("voice.webm", audio_bytes, mime_type or "audio/webm"),
        }
        data = {
            "model": model,
            "response_format": "verbose_json",
            "temperature": "0",
        }
        # Providing the language hint to Whisper dramatically improves accuracy
        # for Indian languages — skips auto-detection and uses the right acoustic model
        if whisper_lang:
            data["language"] = whisper_lang

        headers = {
            "Authorization": f"Bearer {self.groq_api_key}",
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers=headers,
                files=files,
                data=data,
            )
            response.raise_for_status()
            payload = response.json()

        # Whisper verbose_json returns full language names ("malayalam", "english", "hindi").
        # Map to ISO 639-1 2-letter codes so the frontend can use them correctly.
        _WHISPER_LANG_TO_CODE = {
            "english": "en", "malayalam": "ml", "tamil": "ta", "telugu": "te",
            "kannada": "kn", "hindi": "hi", "bengali": "bn", "gujarati": "gu",
            "punjabi": "pa", "marathi": "mr", "urdu": "ur", "arabic": "ar",
            "french": "fr", "spanish": "es", "portuguese": "pt", "german": "de",
            "chinese": "zh", "japanese": "ja", "korean": "ko", "russian": "ru",
        }
        transcript = str(payload.get("text", "")).strip()
        lang_raw = str(payload.get("language") or "unknown").lower().strip()
        if "-" in lang_raw:
            lang_raw = lang_raw.split("-")[0]
        language = _WHISPER_LANG_TO_CODE.get(lang_raw, lang_raw[:2] if len(lang_raw) >= 2 else "unknown")

        confidence = 0.85 if transcript else 0.0
        if isinstance(payload.get("segments"), list) and payload["segments"]:
            avg_logprob_values = [seg.get("avg_logprob") for seg in payload["segments"] if isinstance(seg, dict)]
            numeric = [float(v) for v in avg_logprob_values if isinstance(v, (float, int))]
            if numeric:
                confidence = max(0.0, min(1.0, 1.0 + (sum(numeric) / len(numeric)) / 5.0))

        return transcript, confidence, language
