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
}
