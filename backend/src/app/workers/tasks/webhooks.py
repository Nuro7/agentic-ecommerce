"""Celery task — process queued webhook events."""
from ..celery_app import celery_app


@celery_app.task(name="src.app.workers.tasks.webhooks.process_pending")
def process_pending():
    # TODO: use asyncio.run() to call WebhookService.process_pending()
    pass
