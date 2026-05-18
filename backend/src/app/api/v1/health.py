from fastapi import APIRouter
from ...core.cache import get_redis

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    try:
        r = get_redis()
        await r.ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    return {"status": "ok", "redis": redis_ok}
