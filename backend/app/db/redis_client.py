"""
ChisCode — Redis Client (Upstash HTTP SDK)
==========================================
Uses upstash_redis.asyncio.Redis — the official async Upstash client.

Why this instead of redis-py + rediss://?
  - Upstash's HTTP REST API works through any firewall/proxy (including HF Spaces).
  - No raw TCP socket needed — pure HTTPS, no TLS cert wrestling.
  - Credentials are UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN (set in HF Secrets).
  - upstash_redis.asyncio is fully async — safe to use inside FastAPI coroutines.

HF Spaces Secrets to set:
    UPSTASH_REDIS_REST_URL   = https://<your-db>.upstash.io
    UPSTASH_REDIS_REST_TOKEN = <your-token>
"""

from upstash_redis.asyncio import Redis

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_redis: Redis | None = None


# ── Lifecycle ────────────────────────────────────────────────────

async def connect() -> None:
    """Initialise the Upstash Redis client and verify connectivity."""
    global _redis

    logger.info("Connecting to Upstash Redis", url=settings.upstash_redis_rest_url)

    _redis = Redis(
        url=settings.upstash_redis_rest_url,
        token=settings.upstash_redis_rest_token,
    )

    result = await _redis.ping()
    if result is not True and result != "PONG":
        raise ConnectionError(f"Upstash Redis ping failed: {result!r}")

    logger.info("Upstash Redis connected")


async def disconnect() -> None:
    """Release the Upstash client."""
    global _redis
    if _redis:
        _redis = None
        logger.info("Upstash Redis client released")


def get_redis() -> Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialised — call connect() first.")
    return _redis


# ── Rate Limiting ────────────────────────────────────────────────

RATE_LIMIT_KEY = "rate:{user_id}:{date}"


async def check_and_increment_rate_limit(
    user_id: str,
    daily_limit: int,
    date_str: str,
) -> tuple[bool, int, int]:
    """
    Atomically check and increment the daily request counter.
    Pipeline sends incr + expire in one HTTP round-trip.

    Returns: (allowed, current_count, limit)
    """
    r = get_redis()
    key = RATE_LIMIT_KEY.format(user_id=user_id, date=date_str)

    pipe = r.pipeline()
    pipe.incr(key)
    pipe.expire(key, 86400)
    results = await pipe.execute()

    current = int(results[0])
    allowed = current <= daily_limit
    return allowed, current, daily_limit


async def get_current_usage(user_id: str, date_str: str) -> int:
    """Return today's request count for a user."""
    r = get_redis()
    key = RATE_LIMIT_KEY.format(user_id=user_id, date=date_str)
    val = await r.get(key)
    return int(val) if val else 0


# ── Token Blacklisting ───────────────────────────────────────────

async def blacklist_token(jti: str, ttl_seconds: int) -> None:
    """Add a JWT ID to the blacklist with TTL matching the token's lifetime."""
    await get_redis().setex(f"blacklist:{jti}", ttl_seconds, "1")


async def is_token_blacklisted(jti: str) -> bool:
    """Return True if this JWT ID has been revoked."""
    result = await get_redis().exists(f"blacklist:{jti}")
    return bool(result)


# ── General Cache ────────────────────────────────────────────────

async def cache_set(key: str, value: str, ttl: int = 300) -> None:
    await get_redis().setex(key, ttl, value)


async def cache_get(key: str) -> str | None:
    val = await get_redis().get(key)
    return str(val) if val is not None else None


async def cache_delete(key: str) -> None:
    await get_redis().delete(key)


# ── WebSocket Presence ───────────────────────────────────────────

async def set_user_presence(project_id: str, user_id: str, data: str) -> None:
    """Mark a user as active on a project (120s TTL, refreshed by heartbeat)."""
    r = get_redis()
    await r.hset(f"presence:{project_id}", values={user_id: data})
    await r.expire(f"presence:{project_id}", 120)


async def remove_user_presence(project_id: str, user_id: str) -> None:
    await get_redis().hdel(f"presence:{project_id}", user_id)


async def get_project_presence(project_id: str) -> dict[str, str]:
    result = await get_redis().hgetall(f"presence:{project_id}")
    return result or {}
