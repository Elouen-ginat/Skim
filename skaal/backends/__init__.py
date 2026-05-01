"""Skaal storage backends.

Every backend name is resolved through :mod:`skaal.plugins`, which combines the
first-party backend map with any third-party backends installed via the
``skaal.backends`` entry-point group.

Importing a specific backend directly still works — the ``from skaal.backends
import RedisBackend`` form is preserved for backward compatibility through a
``__getattr__`` shim that delegates to the plugin registry.
"""

from __future__ import annotations

from skaal.backends.base import StorageBackend
from skaal.backends.local_backend import LocalMap
from skaal.plugins import get_backend

# Only the zero-dependency backends are re-exported eagerly.  Everything else
# is loaded lazily via ``__getattr__`` to keep optional dependencies optional.
__all__ = [
    "ChromaVectorBackend",
    "DynamoBackend",
    "LocalMap",
    "PostgresBackend",
    "PgVectorBackend",
    "RedisBackend",
    "RedisStreamChannel",
    "SqliteBackend",
    "StorageBackend",
]


_LEGACY_ALIASES: dict[str, str] = {
    # Module-attribute name → plugin-registry name
    "ChromaVectorBackend": "chroma",
    "DynamoBackend": "dynamodb",
    "PostgresBackend": "postgres",
    "PgVectorBackend": "pgvector",
    "RedisBackend": "redis",
    "SqliteBackend": "sqlite",
}


def __getattr__(name: str) -> object:
    if name in _LEGACY_ALIASES:
        return get_backend(_LEGACY_ALIASES[name])
    if name == "RedisStreamChannel":
        # Channel backend, not a storage backend — left untouched for now;
        # channel-plugin migration follows the same pattern in skaal.channel.
        from skaal.backends.redis_channel import RedisStreamChannel

        return RedisStreamChannel
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
