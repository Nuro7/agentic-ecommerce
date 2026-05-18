"""Celery task — monthly subscription invoicing."""
from ..celery_app import celery_app


@celery_app.task(name="src.app.workers.tasks.billing.run_monthly_invoicing")
def run_monthly_invoicing():
    # TODO: iterate active subscriptions, charge via payment gateway
    pass
