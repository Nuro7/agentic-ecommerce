"""Error handler — converts AppError and unhandled exceptions to JSON responses."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.context import get_request_id, get_tenant_id
from app.core.exceptions import AppError
from app.core.logging import get_logger

logger = get_logger("http.error")


def _problem_details(
    *,
    status: int,
    title: str,
    detail: str,
    error_code: str,
    instance: str,
    extra: dict | None = None,
) -> JSONResponse:
    body = {
        "type": f"https://docs.agentic-commerce.com/errors/{error_code}",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": instance,
    }
    if rid := get_request_id():
        body["request_id"] = rid
    if tid := get_tenant_id():
        body["tenant_id"] = str(tid)
    if extra:
        body["extra"] = extra
    return JSONResponse(status_code=status, content=body, media_type="application/problem+json")


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        logger.warning(
            "app_error",
            error_code=exc.error_code,
            status=exc.status_code,
            detail=exc.detail,
        )
        return _problem_details(
            status=exc.status_code,
            title=exc.title,
            detail=exc.detail,
            error_code=exc.error_code,
            instance=request.url.path,
            extra=exc.extra or None,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_exception", path=request.url.path)
        return _problem_details(
            status=500,
            title="Internal Server Error",
            detail="An unexpected error occurred. Please try again or contact support.",
            error_code="internal_error",
            instance=request.url.path,
        )
