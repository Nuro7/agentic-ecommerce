"""Celery beat periodic task schedule."""
from celery.schedules import crontab
from .celery_app import celery_app

celery_app.conf.beat_schedule = {
    "process-pending-webhooks": {
        "task": "src.app.workers.tasks.webhooks.process_pending",
        "schedule": 60.0,
    },
    "aggregate-daily-analytics": {
        "task": "src.app.workers.tasks.analytics.aggregate_daily",
        "schedule": crontab(hour=0, minute=5),
    },
    "invoice-subscriptions": {
        "task": "src.app.workers.tasks.billing.run_monthly_invoicing",
        "schedule": crontab(day_of_month=1, hour=2, minute=0),
    },
    # Nightly product sync — populates product_cache for BM25 + vector retrieval.
    # Runs at 02:30 UTC after analytics (02:05) and invoicing (02:00) have finished.
    "sync-products-nightly": {
        "task": "src.app.workers.tasks.sync_products.sync_products",
        "schedule": crontab(hour=2, minute=30),
    },
    # Retry failed mutation tool calls (add_to_cart, apply_coupon, etc.).
    # Drains speako:retry_queue every 60 s; items that permanently fail are
    # moved to the dead-letter set (speako:retry_dead) after 3 attempts.
    "retry-failed-actions": {
        "task": "src.app.workers.tasks.retry_actions.retry_failed_actions",
        "schedule": 60.0,
    },
    # Incremental diff sync — runs every 4 h between nightly full syncs.
    # Only upserts products modified since the last cached_at; falls back to a
    # full sync when the platform/cache product counts diverge by > 10 %.
    "sync-products-diff": {
        "task": "src.app.workers.tasks.sync_products.sync_products_diff",
        "schedule": crontab(minute=0, hour="6,10,14,18,22"),
    },
}
