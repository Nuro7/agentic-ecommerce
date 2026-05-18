from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from .models import Conversation, Message


class ConversationRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_or_create(self, tenant_id: str, session_id: str, visitor_id: str | None = None) -> Conversation:
        result = await self.db.execute(
            select(Conversation).where(
                Conversation.tenant_id == tenant_id,
                Conversation.session_id == session_id,
            )
        )
        conv = result.scalar_one_or_none()
        if not conv:
            conv = Conversation(tenant_id=tenant_id, session_id=session_id, visitor_id=visitor_id)
            self.db.add(conv)
            await self.db.commit()
            await self.db.refresh(conv)
        return conv

    async def add_message(self, conversation_id: str, role: str, content: str) -> Message:
        msg = Message(conversation_id=conversation_id, role=role, content=content)
        self.db.add(msg)
        await self.db.commit()
        await self.db.refresh(msg)
        return msg

    async def get_history(self, conversation_id: str, limit: int = 20) -> list[Message]:
        result = await self.db.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at)
            .limit(limit)
        )
        return list(result.scalars().all())
