"""Skaal storage backends.

Every backend name is resolved through :mod:`skaal.plugins`, which merges the
built-in backends declared in ``pyproject.toml`` (``[project.entry-points."skaal.backends"]``)
with any third-party backends installed alongside Skaal and any in-process
registrations made via :func:`skaal.plugins.register_backend`.

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
    "DynamoBackend",
    "LocalMap",
    "PostgresBackend",
    "RedisBackend",
    "RedisStreamChannel",
    "SqliteBackend",
    "StorageBackend",
]


_LEGACY_ALIASES: dict[str, str] = {
    # Module-attribute name → plugin-registry name
    "DynamoBackend": "dynamodb",
    "PostgresBackend": "postgres",
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
