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

from typing import Optional, Dict, Tuple, Union
from upstash_redis.asyncio import Redis

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_redis: Optional[Redis] = None


# ── Lifecycle ────────────────────────────────────────────────────

async def connect() -> None:
    """Initialise the Upstash Redis client and verify connectivity."""
    global _redis

    if not settings.upstash_redis_rest_url or not settings.upstash_redis_rest_token:
        logger.warning("Upstash Redis credentials not set - Redis features disabled")
        return

    try:
        logger.info(f"Connecting to Upstash Redis: {settings.upstash_redis_rest_url}")

        _redis = Redis(
            url=settings.upstash_redis_rest_url,
            token=settings.upstash_redis_rest_token,
        )

        # Verify connection
        result = await _redis.ping()
        
        # Upstash returns True, "PONG", or 1 depending on version
        if result not in (True, "PONG", 1, b"PONG"):
            raise ConnectionError(f"Upstash Redis ping failed: {result!r}")

        logger.info("✅ Upstash Redis connected successfully")
        
    except Exception as e:
        logger.error(f"❌ Failed to connect to Upstash Redis: {str(e)}")
        _redis = None
        raise


async def disconnect() -> None:
    """Release the Upstash client."""
    global _redis
    if _redis is not None:
        try:
            # Upstash HTTP client doesn't need explicit close, but we can cleanup
            _redis = None
            logger.info("Upstash Redis client released")
        except Exception as e:
            logger.warning(f"Error during Redis disconnect: {e}")


def get_redis() -> Redis:
    """Get the Redis client instance."""
    if _redis is None:
        raise RuntimeError(
            "Redis not initialised — call connect() first or check credentials."
        )
    return _redis


def is_connected() -> bool:
    """Check if Redis is connected."""
    return _redis is not None


# ── Rate Limiting ────────────────────────────────────────────────

RATE_LIMIT_KEY = "rate:{user_id}:{date}"


async def check_and_increment_rate_limit(
    user_id: str,
    daily_limit: int,
    date_str: str,
) -> Tuple[bool, int, int]:
    """
    Atomically check and increment the daily request counter.
    Pipeline sends incr + expire in one HTTP round-trip.

    Returns: (allowed, current_count, limit)
    """
    if not is_connected():
        # If Redis is down, allow request but log warning
        logger.warning("Redis not connected - rate limiting disabled")
        return (True, 0, daily_limit)
    
    try:
        r = get_redis()
        key = RATE_LIMIT_KEY.format(user_id=user_id, date=date_str)

        # Use pipeline for atomic operations
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, 86400)  # 24 hours
        results = await pipe.execute()

        # Extract count from results
        # Results is a list: [incr_result, expire_result]
        current = int(results[0]) if results and len(results) > 0 else 0
        allowed = current <= daily_limit
        
        return (allowed, current, daily_limit)
        
    except Exception as e:
        logger.error(f"Rate limit check failed: {e}")
        # On error, allow the request
        return (True, 0, daily_limit)


async def get_current_usage(user_id: str, date_str: str) -> int:
    """Return today's request count for a user."""
    if not is_connected():
        return 0
    
    try:
        r = get_redis()
        key = RATE_LIMIT_KEY.format(user_id=user_id, date=date_str)
        val = await r.get(key)
        
        if val is None:
            return 0
        
        # Handle different return types
        if isinstance(val, (int, str)):
            return int(val)
        elif isinstance(val, bytes):
            return int(val.decode())
        
        return 0
        
    except Exception as e:
        logger.error(f"Failed to get current usage: {e}")
        return 0


async def reset_rate_limit(user_id: str, date_str: str) -> None:
    """Reset rate limit for a user (admin only)."""
    if not is_connected():
        return
    
    try:
        r = get_redis()
        key = RATE_LIMIT_KEY.format(user_id=user_id, date=date_str)
        await r.delete(key)
        logger.info(f"Rate limit reset for user {user_id} on {date_str}")
    except Exception as e:
        logger.error(f"Failed to reset rate limit: {e}")


# ── Token Blacklisting ───────────────────────────────────────────

async def blacklist_token(jti: str, ttl_seconds: int) -> None:
    """Add a JWT ID to the blacklist with TTL matching the token's lifetime."""
    if not is_connected():
        logger.warning("Cannot blacklist token - Redis not connected")
        return
    
    try:
        await get_redis().setex(f"blacklist:{jti}", ttl_seconds, "1")
        logger.debug(f"Token blacklisted: {jti}")
    except Exception as e:
        logger.error(f"Failed to blacklist token: {e}")


async def is_token_blacklisted(jti: str) -> bool:
    """Return True if this JWT ID has been revoked."""
    if not is_connected():
        # If Redis is down, don't block users
        return False
    
    try:
        result = await get_redis().exists(f"blacklist:{jti}")
        # Result can be 0, 1, or boolean depending on version
        return bool(result) and result != 0
    except Exception as e:
        logger.error(f"Failed to check token blacklist: {e}")
        # On error, don't block the user
        return False


async def clear_blacklisted_token(jti: str) -> None:
    """Remove a token from blacklist (for testing/admin)."""
    if not is_connected():
        return
    
    try:
        await get_redis().delete(f"blacklist:{jti}")
    except Exception as e:
        logger.error(f"Failed to clear blacklisted token: {e}")


# ── General Cache ────────────────────────────────────────────────

async def cache_set(key: str, value: str, ttl: int = 300) -> None:
    """Set a cache value with TTL (default 5 minutes)."""
    if not is_connected():
        return
    
    try:
        await get_redis().setex(key, ttl, value)
    except Exception as e:
        logger.error(f"Cache set failed for key {key}: {e}")


async def cache_get(key: str) -> Optional[str]:
    """Get a cached value."""
    if not is_connected():
        return None
    
    try:
        val = await get_redis().get(key)
        
        if val is None:
            return None
        
        # Handle different return types
        if isinstance(val, str):
            return val
        elif isinstance(val, bytes):
            return val.decode('utf-8')
        
        return str(val)
        
    except Exception as e:
        logger.error(f"Cache get failed for key {key}: {e}")
        return None


async def cache_delete(key: str) -> None:
    """Delete a cached value."""
    if not is_connected():
        return
    
    try:
        await get_redis().delete(key)
    except Exception as e:
        logger.error(f"Cache delete failed for key {key}: {e}")


async def cache_exists(key: str) -> bool:
    """Check if a key exists in cache."""
    if not is_connected():
        return False
    
    try:
        result = await get_redis().exists(key)
        return bool(result) and result != 0
    except Exception as e:
        logger.error(f"Cache exists check failed for key {key}: {e}")
        return False


# ── WebSocket Presence ───────────────────────────────────────────

async def set_user_presence(project_id: str, user_id: str, data: str) -> None:
    """Mark a user as active on a project (120s TTL, refreshed by heartbeat)."""
    if not is_connected():
        return
    
    try:
        r = get_redis()
        key = f"presence:{project_id}"
        
        # Use mapping parameter for upstash_redis
        await r.hset(key, mapping={user_id: data})
        await r.expire(key, 120)
        
    except Exception as e:
        logger.error(f"Failed to set user presence: {e}")


async def remove_user_presence(project_id: str, user_id: str) -> None:
    """Remove user from project presence."""
    if not is_connected():
        return
    
    try:
        await get_redis().hdel(f"presence:{project_id}", user_id)
    except Exception as e:
        logger.error(f"Failed to remove user presence: {e}")


async def get_project_presence(project_id: str) -> Dict[str, str]:
    """Get all users present on a project."""
    if not is_connected():
        return {}
    
    try:
        result = await get_redis().hgetall(f"presence:{project_id}")
        
        if result is None:
            return {}
        
        # Convert bytes keys/values to strings if needed
        if isinstance(result, dict):
            return {
                (k.decode() if isinstance(k, bytes) else str(k)): 
                (v.decode() if isinstance(v, bytes) else str(v))
                for k, v in result.items()
            }
        
        return {}
        
    except Exception as e:
        logger.error(f"Failed to get project presence: {e}")
        return {}


async def clear_project_presence(project_id: str) -> None:
    """Clear all presence data for a project."""
    if not is_connected():
        return
    
    try:
        await get_redis().delete(f"presence:{project_id}")
    except Exception as e:
        logger.error(f"Failed to clear project presence: {e}")


# ── API Key Tracking ──────────────────────────────────────────────

async def update_api_key_last_used(user_id: str) -> None:
    """Update last used timestamp for API key."""
    if not is_connected():
        return
    
    try:
        import time
        timestamp = int(time.time())
        await cache_set(f"api_key_last_used:{user_id}", str(timestamp), ttl=86400 * 30)
    except Exception as e:
        logger.error(f"Failed to update API key timestamp: {e}")


async def get_api_key_last_used(user_id: str) -> Optional[int]:
    """Get last used timestamp for API key."""
    if not is_connected():
        return None
    
    try:
        result = await cache_get(f"api_key_last_used:{user_id}")
        return int(result) if result else None
    except Exception as e:
        logger.error(f"Failed to get API key timestamp: {e}")
        return None


# ── Health Check ──────────────────────────────────────────────────

async def health_check() -> Dict[str, Union[bool, str]]:
    """Check Redis connection health."""
    if not is_connected():
        return {
            "connected": False,
            "status": "not_initialized",
            "error": "Redis client not initialized"
        }
    
    try:
        result = await get_redis().ping()
        
        return {
            "connected": True,
            "status": "healthy",
            "ping": str(result)
        }
    except Exception as e:
        return {
            "connected": False,
            "status": "error",
            "error": str(e)
        }
