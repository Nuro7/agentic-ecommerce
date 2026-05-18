"""Celery task — daily analytics aggregation."""
from ..celery_app import celery_app


@celery_app.task(name="src.app.workers.tasks.analytics.aggregate_daily")
def aggregate_daily():
    # TODO: query conversations/orders for yesterday, write ConversationMetric rows
    pass
