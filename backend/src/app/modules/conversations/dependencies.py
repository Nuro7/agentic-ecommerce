from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from .service import ConversationService
from ...core.database import get_db


def get_conversation_service(db: AsyncSession = Depends(get_db)) -> ConversationService:
    return ConversationService(db)
