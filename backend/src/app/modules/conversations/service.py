from sqlalchemy.ext.asyncio import AsyncSession
from .repository import ConversationRepository
from .schemas import ChatRequest, ChatResponse


class ConversationService:
    def __init__(self, db: AsyncSession):
        self.repo = ConversationRepository(db)

    async def chat(self, tenant_id: str, data: ChatRequest) -> ChatResponse:
        conv = await self.repo.get_or_create(tenant_id, data.session_id, data.visitor_id)
        await self.repo.add_message(conv.id, "user", data.message)

        # TODO: route to agent orchestrator (backend/src/app/agent/orchestrator.py)
        reply = "Chat via WebSocket /ws/chat — HTTP fallback not implemented yet."

        await self.repo.add_message(conv.id, "assistant", reply)
        return ChatResponse(reply=reply, session_id=data.session_id)

    async def get_history(self, tenant_id: str, session_id: str) -> list:
        conv = await self.repo.get_or_create(tenant_id, session_id)
        return await self.repo.get_history(conv.id)
