from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from .service import ConversationService
from .schemas import ChatRequest, ChatResponse, MessageOut
from ...core.database import get_db
from ..tenants.dependencies import require_tenant

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.post("/chat", response_model=ChatResponse)
async def chat(
    data: ChatRequest,
    tenant=Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
):
    return await ConversationService(db).chat(tenant.id, data)


@router.get("/{session_id}/history", response_model=list[MessageOut])
async def get_history(
    session_id: str,
    tenant=Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
):
    return await ConversationService(db).get_history(tenant.id, session_id)
