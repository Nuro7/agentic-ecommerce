import redis.asyncio as aioredis
from ..config import settings

redis_client: aioredis.Redis | None = None


async def init_cache() -> None:
    global redis_client
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)


def get_redis() -> aioredis.Redis:
    if redis_client is None:
        raise RuntimeError("Cache not initialised")
    return redis_client
