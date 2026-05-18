from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from .service import BillingService
from ..tenants.dependencies import require_tenant
from ...core.database import get_db


async def check_conversation_quota(
    tenant=Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
):
    service = BillingService(db)
    usage = await service.get_usage(tenant.id, "conversations")
    sub = await service.get_subscription(tenant.id)
    # TODO: compare usage against plan limit and raise 402 if exceeded
    return tenant
