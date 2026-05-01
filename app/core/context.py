"""Request-scoped ContextVars for log enrichment and tenant-aware queries."""

from contextvars import ContextVar
from uuid import UUID

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
tenant_id_var: ContextVar[UUID | None] = ContextVar("tenant_id", default=None)
session_id_var: ContextVar[str | None] = ContextVar("session_id", default=None)


def get_request_id() -> str | None:
    return request_id_var.get()


def get_tenant_id() -> UUID | None:
    return tenant_id_var.get()


def get_session_id() -> str | None:
    return session_id_var.get()
