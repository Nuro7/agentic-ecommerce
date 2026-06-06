"""Celery task — daily analytics aggregation (runs at 00:05 UTC via beat)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from ..celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name="src.app.workers.tasks.analytics.aggregate_daily",
    bind=True,
    max_retries=2,
    default_retry_delay=300,
)
def aggregate_daily(self) -> dict:
    """Aggregate yesterday's conversations and orders into conversation_metrics.

    Runs nightly at 00:05 UTC. Writes one ConversationMetric row per tenant.
    Safe to re-run — upserts on (tenant_id, date).
    """
    try:
        result = asyncio.run(_aggregate_async())
        logger.info("Analytics task: aggregated %d tenants", result["tenants"])
        return result
    except Exception as exc:
        logger.error("Analytics aggregation failed: %s", exc, exc_info=True)
        raise self.retry(exc=exc)


async def _aggregate_async() -> dict:
    from ...core.database import AsyncSessionLocal
    from ...modules.analytics.repository import AnalyticsRepository
    from ...modules.tenants.service import TenantService

    yesterday_start = (
        datetime.now(tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        - timedelta(days=1)
    )
    yesterday_end = yesterday_start + timedelta(days=1)

    tenants_aggregated = 0

    async with AsyncSessionLocal() as db:
        tenant_service = TenantService(db)
        analytics_repo = AnalyticsRepository(db)

        tenants = await tenant_service.list_tenants()
        for tenant in tenants:
            try:
                await analytics_repo.upsert_daily_metric(
                    tenant_id=tenant.id,
                    date=yesterday_start,
                    from_date=yesterday_start,
                    to_date=yesterday_end,
                )
                tenants_aggregated += 1
            except Exception as exc:
                logger.warning("Failed to aggregate tenant=%s: %s", tenant.id, exc)

        await db.commit()

    return {"tenants": tenants_aggregated, "date": yesterday_start.date().isoformat()}
