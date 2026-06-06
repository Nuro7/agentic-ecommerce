from __future__ import annotations

import asyncio
import json
import os
from typing import AsyncIterator, Tuple

import httpx


class STTService:
    def __init__(self) -> None:
        self.deepgram_api_key = os.getenv("DEEPGRAM_API_KEY", "").strip()
        self.groq_api_key     = os.getenv("GROQ_API_KEY", "").strip()
        # xAI Grok STT — reads GROK_API_KEY (xai-...)
        self.grok_xai_api_key = os.getenv("GROK_API_KEY", "").strip()
        self.provider = self._resolve_provider(os.getenv("STT_PROVIDER", "groq").strip().lower())
        self.timeout  = httpx.Timeout(25.0, connect=5.0)

    def _resolve_provider(self, requested: str) -> str:
        if requested == "grok" and self.grok_xai_api_key:
            return "grok"
        if requested == "deepgram" and self.deepgram_api_key:
            return "deepgram"
        if requested == "groq" and self.groq_api_key:
            return "groq"
        # Auto-select best available
        if self.grok_xai_api_key:
            return "grok"
        if self.groq_api_key:
            return "groq"
        if self.deepgram_api_key:
            return "deepgram"
        return "none"

    async def transcribe(self, audio_bytes: bytes, mime_type: str, language_hint: str = "") -> Tuple[str, float, str]:
        if os.getenv("MOCK_SERVICES", "false").lower() == "true":
            return "Mock: I would like to buy some shoes.", 0.95, "en"

        if self.provider == "grok":
            return await self._transcribe_grok_xai(audio_bytes, mime_type, language_hint=language_hint)
        if self.provider == "groq":
            return await self._transcribe_groq(audio_bytes, mime_type, language_hint=language_hint)
        if self.provider == "deepgram":
            return await self._transcribe_deepgram(audio_bytes, mime_type, language_hint=language_hint)
        raise RuntimeError("No STT provider configured. Set GROK_API_KEY, GROQ_API_KEY, or DEEPGRAM_API_KEY.")

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

    async def _transcribe_grok_xai(
        self, audio_bytes: bytes, mime_type: str, language_hint: str = ""
    ) -> Tuple[str, float, str]:
        """
        xAI Grok STT — https://api.x.ai/v1/stt
        Key: GROK_API_KEY (starts with xai-)
        Returns: (transcript, confidence, language_code)
        """
        # Language mapping to ISO 639-1 for consistency
        _LANG_MAP = {
            "english": "en", "malayalam": "ml", "tamil": "ta", "telugu": "te",
            "kannada": "kn", "hindi": "hi", "bengali": "bn", "gujarati": "gu",
            "punjabi": "pa", "arabic": "ar", "french": "fr", "spanish": "es",
            "portuguese": "pt", "german": "de", "chinese": "zh",
            "japanese": "ja", "korean": "ko",
        }

        # Determine file extension from mime_type
        ext = "wav"
        if "webm" in mime_type:
            ext = "webm"
        elif "mp3" in mime_type or "mpeg" in mime_type:
            ext = "mp3"
        elif "ogg" in mime_type:
            ext = "ogg"

        form_data: dict[str, str] = {"format": "true"}
        if language_hint:
            form_data["language"] = language_hint

        headers = {"Authorization": f"Bearer {self.grok_xai_api_key}"}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                "https://api.x.ai/v1/stt",
                headers=headers,
                files={"file": (f"audio.{ext}", audio_bytes, mime_type)},
                data=form_data,
            )
            response.raise_for_status()
            payload = response.json()

        transcript = str(payload.get("text", "")).strip()
        duration   = float(payload.get("duration", 0.0) or 0.0)

        # Grok STT doesn't return a confidence score — estimate from duration
        confidence = 0.88 if transcript and duration > 0.2 else 0.0

        # Detect language from transcript words if available (Grok returns word-level data)
        detected_lang = language_hint or "en"
        words = payload.get("words", [])
        if words and isinstance(words, list):
            # Grok may return language per word — take first word's language if present
            first_word_lang = words[0].get("language", "") if words else ""
            if first_word_lang:
                detected_lang = _LANG_MAP.get(
                    first_word_lang.lower(), first_word_lang[:2].lower()
                )

        return transcript, confidence, detected_lang

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


# ── xAI Grok WebSocket Streaming STT ─────────────────────────────────────────

class GrokStreamingSTT:
    """
    xAI Grok real-time STT via WebSocket.
    Endpoint: wss://api.x.ai/v1/stt

    One instance per Pipeline B voice session. Lifecycle:
        stt = GrokStreamingSTT(api_key, language="en")
        await stt.connect()               # handshake → transcript.created
        await stt.send_audio(pcm_bytes)   # raw PCM16 mono 16kHz, no WAV header
        async for event in stt.events():  # transcript.partial / transcript.done
            ...
        await stt.close()                 # sends audio.done → connection closes

    Key events from xAI:
        transcript.partial  is_final=false, speech_final=false → interim
        transcript.partial  is_final=true,  speech_final=false → chunk-final
        transcript.partial  is_final=true,  speech_final=true  → utterance-final
        transcript.done                                         → session end
    """

    _WS_BASE = "wss://api.x.ai/v1/stt"

    def __init__(
        self,
        api_key: str,
        *,
        language: str = "en",
        endpointing: int = 500,       # ms of silence → utterance boundary
        interim_results: bool = True,
    ) -> None:
        self._api_key     = api_key
        self._language    = language
        self._endpointing = endpointing
        self._interim     = interim_results
        self._ws          = None
        self._connected   = False
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._recv_task: asyncio.Task | None = None

    # ── Connect ───────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        import websockets  # lazy import — only needed for Pipeline B

        params = (
            f"?sample_rate=16000&encoding=pcm"
            f"&interim_results={'true' if self._interim else 'false'}"
            f"&endpointing={self._endpointing}"
        )
        if self._language:
            params += f"&language={self._language}"

        url     = self._WS_BASE + params
        headers = {"Authorization": f"Bearer {self._api_key}"}

        self._ws = await websockets.connect(url, additional_headers=headers)

        # xAI sends transcript.created before accepting audio
        raw = await self._ws.recv()
        msg = json.loads(raw)
        if msg.get("type") != "transcript.created":
            await self._ws.close()
            raise RuntimeError(
                f"xAI Grok STT handshake failed — expected 'transcript.created', "
                f"got: {msg}"
            )

        self._connected = True
        self._recv_task = asyncio.create_task(self._receive_loop())

    # ── Audio input ───────────────────────────────────────────────────────────

    async def send_audio(self, pcm_chunk: bytes) -> None:
        """Send raw PCM16 mono 16kHz chunk. No WAV header needed."""
        if self._ws and self._connected:
            try:
                await self._ws.send(pcm_chunk)
            except Exception:
                self._connected = False

    # ── Close ─────────────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Send audio.done, cancel receiver, close WebSocket."""
        if self._ws and self._connected:
            try:
                await self._ws.send(json.dumps({"type": "audio.done"}))
            except Exception:
                pass
        self._connected = False

        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        # Unblock any consumer waiting on events()
        await self._event_queue.put(None)

    # ── Internal receiver loop ────────────────────────────────────────────────

    async def _receive_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    event = json.loads(raw)
                except Exception:
                    continue
                await self._event_queue.put(event)
                if event.get("type") == "transcript.done":
                    break
        except Exception:
            pass
        finally:
            await self._event_queue.put(None)

    # ── Event iterator ────────────────────────────────────────────────────────

    async def events(self) -> AsyncIterator[dict]:
        """Yield transcript events until the WS closes or close() is called."""
        while True:
            event = await self._event_queue.get()
            if event is None:
                break
            yield event
