"""Skaal storage backends.

Every backend name is resolved through :mod:`skaal.plugins`, which merges the
built-in backend specs declared in ``pyproject.toml``
(``[project.entry-points."skaal.backend_specs"]``) with any third-party backends
installed alongside Skaal and any in-process
registrations made via :func:`skaal.plugins.register_runtime_backend`.

Importing a specific backend directly still works — the ``from skaal.backends
import RedisBackend`` form is preserved for backward compatibility through a
``__getattr__`` shim that delegates to the plugin registry.
"""

from __future__ import annotations

from skaal.backends.base import StorageBackend
from skaal.backends.kv.dynamodb import DYNAMODB_SPEC
from skaal.backends.kv.firestore import FIRESTORE_SPEC
from skaal.backends.kv.local_map import LOCAL_MAP_SPEC, LocalMap
from skaal.backends.kv.postgres import CLOUD_SQL_POSTGRES_SPEC, RDS_POSTGRES_SPEC
from skaal.backends.kv.redis import LOCAL_REDIS_SPEC, MEMORYSTORE_REDIS_SPEC
from skaal.backends.kv.sqlite import SQLITE_SPEC
from skaal.backends.vector.chroma import CHROMA_LOCAL_SPEC
from skaal.backends.vector.pgvector import CLOUD_SQL_PGVECTOR_SPEC, RDS_PGVECTOR_SPEC

# Only the zero-dependency backends are re-exported eagerly.  Everything else
# is loaded lazily via ``__getattr__`` to keep optional dependencies optional.
__all__ = [
    "BUILTIN_BACKENDS",
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


BUILTIN_BACKENDS = (
    CHROMA_LOCAL_SPEC,
    CLOUD_SQL_PGVECTOR_SPEC,
    CLOUD_SQL_POSTGRES_SPEC,
    DYNAMODB_SPEC,
    FIRESTORE_SPEC,
    LOCAL_MAP_SPEC,
    LOCAL_REDIS_SPEC,
    MEMORYSTORE_REDIS_SPEC,
    RDS_PGVECTOR_SPEC,
    RDS_POSTGRES_SPEC,
    SQLITE_SPEC,
)


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
        from skaal.plugins import get_backend

        return get_backend(_LEGACY_ALIASES[name])
    if name == "RedisStreamChannel":
        from skaal.backends.channels.redis import RedisStreamChannel

        return RedisStreamChannel
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
