"""Operator-only admin endpoints (guarded by X-Admin-Token / ADMIN_API_KEY)."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.database import get_db
from ..billing.service import BillingService
from ..billing.schemas import AdminAssignPlanRequest, SubscriptionOut
from ..tenants.repository import TenantRepository
from .dependencies import require_admin

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.post("/subscriptions", response_model=SubscriptionOut)
async def assign_plan(body: AdminAssignPlanRequest, db: AsyncSession = Depends(get_db)):
    """Activate (or change) a tenant's subscription to a plan — no payment (testing)."""
    tenant = await TenantRepository(db).get_by_id(body.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    return await BillingService(db).assign_plan(body.tenant_id, body.plan)


@router.get("/subscriptions/{tenant_id}", response_model=SubscriptionOut)
async def get_tenant_subscription(tenant_id: str, db: AsyncSession = Depends(get_db)):
    """Read any tenant's subscription (operator view, for verification)."""
    return await BillingService(db).get_subscription(tenant_id)
