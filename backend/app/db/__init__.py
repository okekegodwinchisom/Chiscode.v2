"""
ChisCode — Database Package
MongoDB and Redis connections and utilities.
"""

from.db.mongodb import (
    mongodb,
    get_database,
    get_collection,
    users_collection,
    projects_collection,
    project_versions_collection,
    close_mongodb_connection,
    connect_to_mongodb
)

from.db.redis_client import (
    redis_client,
    get_redis,
    close_redis_connection
)

# Convenience exports
__all__ = [
    # MongoDB
    "mongodb",
    "get_database",
    "get_collection",
    "users_collection",
    "projects_collection",
    "project_versions_collection",
    "connect_to_mongodb",
    "close_mongodb_connection",
    
    # Redis
    "redis_client",
    "get_redis",
    "close_redis_connection",
]