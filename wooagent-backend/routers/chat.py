from __future__ import annotations

# ═══════════════════════════════════════════════════════════════════════════════
# /chat — DISABLED (superseded by Gemini 3.1 Flash Live A2A)
# ───────────────────────────────────────────────────────────────────────────────
# All text and audio interaction now flows exclusively through the WebSocket
# endpoint at /wooagent/stream (routers/live.py).
# Gemini 3.1 Flash Live Preview handles STT + reasoning + TTS natively.
# OpenAI / Groq are NOT called anywhere in this architecture.
# ═══════════════════════════════════════════════════════════════════════════════

import logging
from fastapi import APIRouter
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/chat")
async def chat_endpoint_disabled():
    """Legacy HTTP chat endpoint — disabled in favour of Gemini 3.1 Live A2A."""
    logger.debug("POST /chat called — returning 410 (superseded by Live API WebSocket)")
    return JSONResponse(
        status_code=410,
        content={
            "error": "gone",
            "detail": (
                "The /chat HTTP endpoint is disabled. "
                "Connect to /wooagent/stream via WebSocket for real-time voice interaction "
                "powered by Gemini 3.1 Flash Live Preview."
            ),
        },
    )
