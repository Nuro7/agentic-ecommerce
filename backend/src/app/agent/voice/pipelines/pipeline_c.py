"""
Pipeline C — Text-only degraded mode  (LAST RESORT)

Activates when BOTH Pipeline A (Gemini Live) AND Pipeline B (xAI STT + Gemini TTS)
are unavailable (circuit breakers open).

Flow:
  Browser sends {"type":"text_input","text":"..."} via WebSocket
    → Brain: orchestrator.run()  (Gemini 2.5 Flash — same Brain as A and B)
    → response text  → {"type":"transcript","text":"..."}
    → ui_actions     → {"type":"ui_action","action":{...}}
    → suggestions    → {"type":"suggestions","items":[...]}
    → NO audio (both voice pipelines are down)

Widget receives {"type":"pipeline_active","pipeline":"C","voice_disabled":true}
and shows a banner: "Voice unavailable — type your question below."
All shopping logic (products, cart, orders, checkout) still works fully via text.

No circuit breaker on Pipeline C — it is always available as a last resort.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import WebSocketDisconnect

from ....agent.orchestrator import AgentOrchestrator

logger = logging.getLogger(__name__)


class PipelineC:
    """
    Text-only pipeline — no STT, no TTS, Brain + text I/O only.
    Last resort when both A and B are down.
    """

    def __init__(self, session_service: Any) -> None:
        self.session_service = session_service
        self._orchestrators: dict[str, AgentOrchestrator] = {}

    def _get_orchestrator(self, session_id: str, store_client: Any) -> AgentOrchestrator:
        if session_id not in self._orchestrators:
            self._orchestrators[session_id] = AgentOrchestrator(
                store_client=store_client,
                session_service=self.session_service,
                tts_service=None,
            )
        return self._orchestrators[session_id]

    async def run(self, websocket: Any, session_id: str, store_client: Any = None,
                  tenant_id: str = "_dev") -> None:
        """
        Run Pipeline C — text-only mode.
        Activates when both Pipeline A and Pipeline B have failed.
        """
        logger.info(f"Pipeline C active: session={session_id} (both A and B unavailable)")

        # Tell widget: voice is down, switch to text input mode
        try:
            await websocket.send_text(json.dumps({
                "type":          "pipeline_active",
                "pipeline":      "C",
                "voice_disabled": True,
                "message":       "Voice unavailable. Type your question below.",
            }))
        except Exception:
            pass

        orchestrator = self._get_orchestrator(session_id, store_client)

        try:
            while True:
                data = await websocket.receive()

                # Normal client disconnect — exit cleanly, no error
                if data.get("type") == "websocket.disconnect":
                    logger.info(f"Pipeline C: client disconnected session={session_id}")
                    break

                # Binary audio chunks — voice is down, silently discard
                if "bytes" in data:
                    continue

                # Text messages only
                if not ("text" in data and data["text"]):
                    continue

                try:
                    ctrl = json.loads(data["text"])
                except json.JSONDecodeError:
                    continue

                if ctrl.get("type") != "text_input" or not ctrl.get("text"):
                    continue

                query    = ctrl["text"].strip()
                language = ctrl.get("language", "en")

                if not query:
                    continue

                # Echo user message back to widget
                try:
                    await websocket.send_text(json.dumps({
                        "type": "user_transcript", "text": query,
                    }))
                except Exception:
                    pass

                # Brain (Gemini 2.5 Flash)
                try:
                    result = await orchestrator.run(
                        session_id=session_id,
                        user_message=query,
                        language=language,
                        tenant_id=tenant_id,
                    )
                    response_text = (
                        result.get("speech_text")
                        or result.get("text")
                        or result.get("response_text")
                        or ""
                    )
                    ui_actions  = result.get("ui_actions") or result.get("actions") or []
                    suggestions = result.get("suggested_replies") or []

                except Exception as e:
                    logger.error(f"Brain error session={session_id}: {e}", exc_info=True)
                    response_text = "Sorry, something went wrong. Please try again."
                    ui_actions    = []
                    suggestions   = []

                # UI actions → widget (add-to-cart, show-products, etc.)
                for action in ui_actions:
                    if action and action.get("type") not in (None, "noop"):
                        try:
                            await websocket.send_text(json.dumps({
                                "type": "ui_action", "action": action,
                            }))
                        except Exception:
                            pass

                # Suggested replies for quick-tap buttons
                if suggestions:
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "suggestions", "items": suggestions,
                        }))
                    except Exception:
                        pass

                # Text response → widget chat bubble
                if response_text:
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "transcript", "text": response_text,
                        }))
                    except Exception:
                        pass

                # Turn complete signal
                try:
                    await websocket.send_text(json.dumps({"type": "turn_complete"}))
                except Exception:
                    pass

                logger.info(
                    f"Pipeline C turn: [{query[:60]}] → [{response_text[:60]}] "
                    f"session={session_id}"
                )

        except WebSocketDisconnect:
            # Normal disconnect — not an error
            logger.info(f"Pipeline C: WebSocket disconnected session={session_id}")

        except Exception as e:
            # Unexpected error — log and re-raise so router handles cleanup
            logger.error(
                f"Pipeline C unexpected error session={session_id}: "
                f"{type(e).__name__}: {e}",
                exc_info=True,
            )
            raise

        finally:
            self._orchestrators.pop(session_id, None)
            logger.info(f"Pipeline C closed: session={session_id}")
