import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from .service import ConversationService
from .schemas import ChatRequest, ChatResponse, MessageOut
from ...core.database import get_db
from ..billing.dependencies import enforce_conversation_quota
from ..billing.service import BillingService
from ..tenants.dependencies import get_authenticated_tenant

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: Request,
    data: ChatRequest,
    tenant=Depends(get_authenticated_tenant),
    db: AsyncSession = Depends(get_db),
):
    await enforce_conversation_quota(
        tenant.id, db, redis=getattr(request.app.state, "redis", None),
    )
    orchestrator = getattr(request.app.state, "orchestrator", None)
    result = await ConversationService(db).chat(
        tenant.id,
        data,
        orchestrator=orchestrator,
    )
    try:
        await BillingService(db).record_usage(tenant.id, "conversations")
        await db.commit()
    except Exception as exc:
        logger.warning("Failed to record conversation usage for tenant=%s: %s", tenant.id, exc)
    return result


@router.get("/{session_id}/history", response_model=list[MessageOut])
async def get_history(
    session_id: str,
    tenant=Depends(get_authenticated_tenant),
    db: AsyncSession = Depends(get_db),
):
    return await ConversationService(db).get_history(tenant.id, session_id)
