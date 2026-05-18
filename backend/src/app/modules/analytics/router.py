from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from .service import AnalyticsService
from .schemas import AnalyticsSummary, MetricOut
from ...core.database import get_db
from ..tenants.dependencies import require_tenant

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/summary", response_model=AnalyticsSummary)
async def get_summary(tenant=Depends(require_tenant), db: AsyncSession = Depends(get_db)):
    return await AnalyticsService(db).get_summary(tenant.id)


@router.get("/metrics", response_model=list[MetricOut])
async def get_metrics(
    from_date: datetime = Query(default_factory=lambda: datetime.now(timezone.utc) - timedelta(days=30)),
    to_date: datetime = Query(default_factory=lambda: datetime.now(timezone.utc)),
    tenant=Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
):
    return await AnalyticsService(db).get_metrics(tenant.id, from_date, to_date)
