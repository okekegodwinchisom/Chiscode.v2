"""
ChisCode — MongoDB Client
Async Motor client with connection lifecycle management and index creation.
"""
from typing import AsyncGenerator

import motor.motor_asyncio
from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# ── Client singleton ─────────────────────────────────────────

_client: motor.motor_asyncio.AsyncIOMotorClient | None = None
_db: motor.motor_asyncio.AsyncIOMotorDatabase | None = None


def get_client() -> motor.motor_asyncio.AsyncIOMotorClient:
    if _client is None:
        raise RuntimeError("MongoDB client not initialised. Call connect() first.")
    return _client


def get_db() -> motor.motor_asyncio.AsyncIOMotorDatabase:
    if _db is None:
        raise RuntimeError("MongoDB database not initialised. Call connect() first.")
    return _db


# ── Lifecycle ────────────────────────────────────────────────

async def connect() -> None:
    """Create the Motor client and verify connectivity."""
    global _client, _db

    logger.info("Connecting to MongoDB", url=settings.mongodb_url.split("@")[-1])

    _client = motor.motor_asyncio.AsyncIOMotorClient(
        settings.mongodb_url,
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
        maxPoolSize=20,
        minPoolSize=2,
    )

    try:
        await _client.admin.command("ping")
        logger.info("MongoDB connection established")
    except (ConnectionFailure, ServerSelectionTimeoutError) as exc:
        logger.error("MongoDB connection failed", error=str(exc))
        raise

    _db = _client[settings.mongodb_db]
    await _create_indexes()


async def disconnect() -> None:
    """Close the Motor client cleanly."""
    global _client, _db
    if _client:
        _client.close()
        _client = None
        _db = None
        logger.info("MongoDB connection closed")


async def _create_indexes() -> None:
    """Idempotently create all required collection indexes."""
    db = get_db()

    # Users collection
    await db.users.create_indexes([
        IndexModel([("email", ASCENDING)], unique=True, name="email_unique"),
        IndexModel([("github_id", ASCENDING)], sparse=True, name="github_id"),
        IndexModel([("api_key_hash", ASCENDING)], sparse=True, name="api_key"),
        IndexModel([("created_at", DESCENDING)], name="created_at"),
    ])

    # Projects collection
    await db.projects.create_indexes([
        IndexModel([("user_id", ASCENDING)], name="user_id"),
        IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)], name="user_projects"),
        IndexModel([("status", ASCENDING)], name="status"),
    ])

    # Project versions collection
    await db.project_versions.create_indexes([
        IndexModel([("project_id", ASCENDING)], name="project_id"),
        IndexModel([("project_id", ASCENDING), ("version", DESCENDING)], name="project_version", unique=True),
    ])

    # Sessions collection (for refresh tokens)
    await db.sessions.create_indexes([
        IndexModel([("user_id", ASCENDING)], name="user_id"),
        IndexModel([("jti", ASCENDING)], unique=True, name="jti"),
        IndexModel([("expires_at", ASCENDING)], expireAfterSeconds=0, name="ttl"),  # Auto-expire
    ])

    logger.info("MongoDB indexes created")


# ── Collection accessors ─────────────────────────────────────

def users_collection():
    return get_db().users


def projects_collection():
    return get_db().projects


def project_versions_collection():
    return get_db().project_versions


def sessions_collection():
    return get_db().sessions


def templates_collection():
    return get_db().templates