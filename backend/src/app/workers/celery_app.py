"""Celery application — broker and result backend via Redis."""
from celery import Celery
from ..config import settings

celery_app = Celery(
    "agentic_commerce",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "src.app.workers.tasks.billing",
        "src.app.workers.tasks.webhooks",
        "src.app.workers.tasks.analytics",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
)
