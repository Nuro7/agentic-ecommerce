from __future__ import annotations

import logging
from fastapi import APIRouter
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/chat")
async def chat_endpoint_disabled():
    """Legacy HTTP chat endpoint — disabled in favour of Gemini Live WebSocket."""
    logger.debug("POST /chat called — returning 410 (superseded by Live API WebSocket)")
    return JSONResponse(
        status_code=410,
        content={
            "error": "gone",
            "detail": (
                "The /chat HTTP endpoint is disabled. "
                "Connect to /wooagent/stream via WebSocket for real-time voice interaction "
                "powered by Gemini Flash Live."
            ),
        },
    )
