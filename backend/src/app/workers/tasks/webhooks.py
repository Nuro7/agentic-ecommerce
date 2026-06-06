"""Celery task — process queued webhook events (idempotent, retryable)."""
from __future__ import annotations

import asyncio
import logging

from ..celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name="src.app.workers.tasks.webhooks.process_pending",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def process_pending(self) -> dict:
    """Read pending webhook_events rows, dispatch handlers, mark processed.

    Runs every 60 seconds via Celery beat (schedules.py).
    Idempotent — safe to run multiple times; already-processed rows are skipped
    by the repository query (status != 'processed').
    """
    try:
        processed = asyncio.run(_process_pending_async())
        logger.info("Webhook task: processed %d events", processed)
        return {"processed": processed}
    except Exception as exc:
        logger.error("Webhook task failed: %s", exc, exc_info=True)
        raise self.retry(exc=exc)


async def _process_pending_async() -> int:
    from ...core.database import AsyncSessionLocal
    from ...modules.webhooks.service import WebhookService

    async with AsyncSessionLocal() as db:
        service = WebhookService(db)
        count = await service.process_pending()
        await db.commit()
        return count
