"""ORM models package.

Importing this package ensures all models are registered on Base.metadata
so Alembic can see them during autogenerate.
"""

from app.infrastructure.persistence.models.billing import BillingInterval, PaymentGateway, Plan
from app.infrastructure.persistence.models.tenant import PlatformKind, Tenant, TenantStatus

__all__ = [
    "Tenant",
    "TenantStatus",
    "PlatformKind",
    "Plan",
    "BillingInterval",
    "PaymentGateway",
]
