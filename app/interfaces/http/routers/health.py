"""Health, readiness, and version endpoints."""

from typing import Any

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.config import settings
from app.core.logging import get_logger
from app.infrastructure.cache.redis_cache import get_redis
from app.infrastructure.persistence.database import db_session

logger = get_logger("health")
router = APIRouter(tags=["operations"])


@router.get("/health", status_code=status.HTTP_200_OK)
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> JSONResponse:
    checks: dict[str, Any] = {}
    overall_ok = True

    try:
        async with db_session() as session:
            await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as e:
        logger.warning("readiness_postgres_failed", error=str(e))
        checks["postgres"] = f"error: {type(e).__name__}"
        overall_ok = False

    try:
        await get_redis().ping()
        checks["redis"] = "ok"
    except Exception as e:
        logger.warning("readiness_redis_failed", error=str(e))
        checks["redis"] = f"error: {type(e).__name__}"
        overall_ok = False

    code = status.HTTP_200_OK if overall_ok else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(
        status_code=code,
        content={"status": "ok" if overall_ok else "degraded", "checks": checks},
    )


@router.get("/version")
async def version() -> dict[str, str]:
    return {
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
    }
