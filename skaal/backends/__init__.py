"""Skaal storage backends — pluggable key-value stores.

Optional backends (Redis, DynamoDB, Postgres) are imported lazily so that
missing optional dependencies do not prevent the core library from loading.
"""

from __future__ import annotations

from skaal.backends.base import StorageBackend
from skaal.backends.local_backend import LocalMap


def __getattr__(name: str) -> object:
    """Lazy import for optional backends that require extra dependencies."""
    if name == "RedisBackend":
        from skaal.backends.redis_backend import RedisBackend

        return RedisBackend
    if name == "DynamoBackend":
        from skaal.backends.dynamodb_backend import DynamoBackend

        return DynamoBackend
    if name == "SqliteBackend":
        from skaal.backends.sqlite_backend import SqliteBackend

        return SqliteBackend
    if name == "PostgresBackend":
        from skaal.backends.postgres_backend import PostgresBackend

        return PostgresBackend
    if name == "RedisStreamChannel":
        from skaal.backends.redis_channel import RedisStreamChannel

        return RedisStreamChannel
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "DynamoBackend",
    "LocalMap",
    "PostgresBackend",
    "RedisBackend",
    "RedisStreamChannel",
    "SqliteBackend",
    "StorageBackend",
]
