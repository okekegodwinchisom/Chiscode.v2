"""
ChisCode — Redis Client
Async Redis client for session caching, rate limiting, and token blacklisting.
"""
from typing import Any

import redis.asyncio as aioredis
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_redis: aioredis.Redis | None = None


# ── Lifecycle ────────────────────────────────────────────────

async def connect() -> None:
    global _redis
    logger.info("Connecting to Redis")
    _redis = await aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
        max_connections=20,
        socket_timeout=5,
        socket_connect_timeout=5,
    )
    await _redis.ping()
    logger.info("Redis connection established")


async def disconnect() -> None:
    global _redis
    if _redis:
        await _redis.aclose()
        _redis = None
        logger.info("Redis connection closed")


def get_redis() -> aioredis.Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialised. Call connect() first.")
    return _redis


# ── Rate Limiting ─────────────────────────────────────────────

RATE_LIMIT_KEY = "rate:{user_id}:{date}"


async def check_and_increment_rate_limit(
    user_id: str,
    daily_limit: int,
    date_str: str,
) -> tuple[bool, int, int]:
    """
    Atomically check and increment the daily request counter.

    Returns:
        (allowed, current_count, limit)
    """
    r = get_redis()
    key = RATE_LIMIT_KEY.format(user_id=user_id, date=date_str)

    pipe = r.pipeline()
    pipe.incr(key)
    pipe.expire(key, 86400)  # 24-hour TTL
    results = await pipe.execute()

    current = results[0]
    allowed = current <= daily_limit
    return allowed, current, daily_limit


async def get_current_usage(user_id: str, date_str: str) -> int:
    """Get current request count for a user on a given date."""
    r = get_redis()
    key = RATE_LIMIT_KEY.format(user_id=user_id, date=date_str)
    val = await r.get(key)
    return int(val) if val else 0


# ── Token Blacklisting ────────────────────────────────────────

async def blacklist_token(jti: str, ttl_seconds: int) -> None:
    """Add a JWT ID to the blacklist with a TTL matching token expiry."""
    r = get_redis()
    await r.setex(f"blacklist:{jti}", ttl_seconds, "1")


async def is_token_blacklisted(jti: str) -> bool:
    """Check whether a JWT ID has been blacklisted (logged out)."""
    r = get_redis()
    return bool(await r.exists(f"blacklist:{jti}"))


# ── General Cache ─────────────────────────────────────────────

async def cache_set(key: str, value: str, ttl: int = 300) -> None:
    await get_redis().setex(key, ttl, value)


async def cache_get(key: str) -> str | None:
    return await get_redis().get(key)


async def cache_delete(key: str) -> None:
    await get_redis().delete(key)


# ── WebSocket presence ────────────────────────────────────────

async def set_user_presence(project_id: str, user_id: str, data: str) -> None:
    """Mark a user as active on a project (TTL: 60s, refreshed by heartbeat)."""
    r = get_redis()
    await r.hset(f"presence:{project_id}", user_id, data)
    await r.expire(f"presence:{project_id}", 120)


async def remove_user_presence(project_id: str, user_id: str) -> None:
    await get_redis().hdel(f"presence:{project_id}", user_id)


async def get_project_presence(project_id: str) -> dict[str, str]:
    return await get_redis().hgetall(f"presence:{project_id}")