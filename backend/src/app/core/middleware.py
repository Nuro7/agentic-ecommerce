import uuid
import time
import logging
from fastapi import Request
from fastapi.responses import JSONResponse
from .exceptions import AppError

logger = logging.getLogger(__name__)


async def request_id_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = round((time.perf_counter() - start) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    logger.info("%s %s %s %.0fms", request.method, request.url.path, response.status_code, elapsed)
    return response


async def app_error_handler(request: Request, exc: AppError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
