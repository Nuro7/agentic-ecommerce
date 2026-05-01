"""Application error hierarchy."""

from typing import Any


class AppError(Exception):
    status_code: int = 500
    error_code: str = "internal_error"
    title: str = "Internal Server Error"

    def __init__(self, detail: str | None = None, **extra: Any) -> None:
        self.detail = detail or self.title
        self.extra = extra
        super().__init__(self.detail)


class ValidationError(AppError):
    status_code = 400
    error_code = "validation_error"
    title = "Validation Error"


class AuthenticationError(AppError):
    status_code = 401
    error_code = "authentication_error"
    title = "Authentication Required"


class AuthorizationError(AppError):
    status_code = 403
    error_code = "authorization_error"
    title = "Forbidden"


class NotFoundError(AppError):
    status_code = 404
    error_code = "not_found"
    title = "Resource Not Found"


class ConflictError(AppError):
    status_code = 409
    error_code = "conflict"
    title = "Conflict"


class RateLimitError(AppError):
    status_code = 429
    error_code = "rate_limit_exceeded"
    title = "Rate Limit Exceeded"


class ExternalServiceError(AppError):
    status_code = 502
    error_code = "external_service_error"
    title = "External Service Error"


class TenantSuspendedError(AuthorizationError):
    error_code = "tenant_suspended"
    title = "Tenant Suspended"


class QuotaExceededError(AppError):
    status_code = 402
    error_code = "quota_exceeded"
    title = "Quota Exceeded"
