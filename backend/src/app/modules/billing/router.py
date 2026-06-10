from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from .service import BillingService
from .schemas import PlanOut, SubscriptionOut
from ...core.database import get_db
from ..tenants.dependencies import get_authenticated_tenant

router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/plans", response_model=list[PlanOut])
async def list_plans(db: AsyncSession = Depends(get_db)):
    return await BillingService(db).list_plans()


@router.get("/subscription", response_model=SubscriptionOut)
async def get_subscription(tenant=Depends(get_authenticated_tenant), db: AsyncSession = Depends(get_db)):
    return await BillingService(db).get_subscription(tenant.id)
