"""Celery application — broker and result backend via Redis."""
from celery import Celery
from ..config import settings

# Use a dedicated broker Redis when configured, so a cache/session stampede on
# the main Redis can't stall task delivery. Falls back to the shared redis_url.
_broker = settings.celery_broker_url or settings.redis_url

celery_app = Celery(
    "agentic_commerce",
    broker=_broker,
    backend=_broker,
    include=[
        "src.app.workers.tasks.billing",
        "src.app.workers.tasks.webhooks",
        "src.app.workers.tasks.analytics",
        "src.app.workers.tasks.sync_products",
        "src.app.workers.tasks.retry_actions",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # ── Reliability / resilience ──────────────────────────────────────────────
    # acks_late + reject_on_worker_lost: a task is only acked after it finishes,
    # so a worker crash re-queues it instead of silently dropping it.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # One task per worker at a time — long syncs can't starve the 60s retry drain.
    worker_prefetch_multiplier=1,
    # Hard/soft time limits so a hung upstream can't pin a worker forever.
    task_time_limit=900,        # 15 min hard kill
    task_soft_time_limit=840,   # 14 min — task can clean up
    # Expire stored results so the Redis result backend doesn't grow unbounded.
    result_expires=3600,
)

# Error tracking for the worker + beat processes (separate from the web process).
# No-op unless SENTRY_DSN is set; sentry-sdk auto-instruments Celery on init.
if settings.sentry_dsn:
    import sentry_sdk
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment or settings.environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
    )

# Register the periodic beat schedule. Importing the module attaches
# beat_schedule to celery_app.conf; without this, `celery beat` runs nothing.
# Imported last (celery_app already defined) to avoid a circular import.
from . import schedules  # noqa: E402,F401
