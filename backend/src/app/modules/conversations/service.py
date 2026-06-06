from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from .repository import ConversationRepository
from .schemas import ChatRequest, ChatResponse

logger = logging.getLogger(__name__)


class ConversationService:
    def __init__(self, db: AsyncSession):
        self.repo = ConversationRepository(db)

    async def chat(
        self,
        tenant_id: str,
        data: ChatRequest,
        orchestrator=None,
        store_client=None,
        session_service=None,
    ) -> ChatResponse:
        """Process a chat message via the agent orchestrator (HTTP fallback path).

        The primary path is WebSocket /wooagent/stream (Gemini Live).
        This HTTP path is used when the WebSocket is unavailable or for
        server-side testing.
        """
        # 1. Persist the conversation record + user message
        conv = await self.repo.get_or_create(tenant_id, data.session_id, data.visitor_id)
        await self.repo.add_message(conv.id, "user", data.message)

        reply: str = ""

        # 2. Route through orchestrator if available
        if orchestrator is not None:
            try:
                result = await orchestrator.run(
                    session_id=data.session_id,
                    user_message=data.message,
                )
                reply = result.get("response_text") or result.get("text") or ""
            except Exception as exc:
                logger.warning("Orchestrator error in HTTP chat fallback: %s", exc)

        # 3. Hard fallback — orchestrator unavailable or failed
        if not reply:
            reply = (
                "I'm your shopping assistant. For the best experience, "
                "please use the voice widget. How can I help you today?"
            )

        # 4. Persist assistant reply
        await self.repo.add_message(conv.id, "assistant", reply)

        return ChatResponse(reply=reply, session_id=data.session_id)

    async def get_history(self, tenant_id: str, session_id: str) -> list:
        conv = await self.repo.get_or_create(tenant_id, session_id)
        return await self.repo.get_history(conv.id)
