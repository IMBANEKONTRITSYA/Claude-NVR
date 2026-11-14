import redis.asyncio as redis
from ..config import settings

_r: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _r
    if _r is None:
        _r = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _r
