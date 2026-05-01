from __future__ import annotations

# ═══════════════════════════════════════════════════════════════════════════════
# /transcribe — DISABLED (superseded by Gemini 3.1 Flash Live A2A)
# ───────────────────────────────────────────────────────────────────────────────
# STT is now handled natively by Gemini 3.1 Flash Live Preview over the
# WebSocket at /wooagent/stream.  Raw PCM audio is streamed directly to Gemini
# via LiveClientRealtimeInput — no separate Groq Whisper / Deepgram step.
# ═══════════════════════════════════════════════════════════════════════════════

import logging
from fastapi import APIRouter
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/transcribe")
async def transcribe_disabled():
    """Legacy STT transcription endpoint — disabled in favour of Gemini 3.1 Live A2A."""
    logger.debug("POST /transcribe called — returning 410 (superseded by Live API WebSocket)")
    return JSONResponse(
        status_code=410,
        content={
            "error": "gone",
            "detail": (
                "The /transcribe HTTP endpoint is disabled. "
                "Audio is transcribed natively by Gemini 3.1 Flash Live Preview "
                "over the WebSocket at /wooagent/stream."
            ),
        },
    )
