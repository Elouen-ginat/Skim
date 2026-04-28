"""Skaal exception hierarchy.

Every backend, plugin, or deploy path that raises an error should surface it
as a subclass of :class:`SkaalError` so callers can write portable
``except`` clauses regardless of which concrete backend is active.

Native exceptions from underlying libraries (``aioredis.WatchError``,
``asyncpg.UniqueViolationError``, ``botocore.exceptions.ClientError``, …)
are wrapped at the backend boundary — never leaked through the protocol.
"""

from __future__ import annotations


class SkaalError(Exception):
    """Base class for every Skaal-originating exception."""


# ── Backend / storage-layer errors ────────────────────────────────────────────


class SkaalBackendError(SkaalError):
    """A storage backend failed an operation."""


class SkaalConflict(SkaalBackendError):
    """An optimistic-concurrency / compare-and-swap update lost the race.

    Raised by ``atomic_update`` implementations when the backing store
    reports that the value changed between the read and the write (Redis
    ``WatchError``, DynamoDB ``ConditionalCheckFailedException``, Firestore
    contention, Postgres serialization failure, …).  Callers may retry.
    """


class SkaalUnavailable(SkaalBackendError):
    """Transient, retriable failure (network blip, pool exhausted, 5xx)."""


# ── Deploy & plugin errors ────────────────────────────────────────────────────


class SkaalDeployError(SkaalError):
    """Deployment packaging, orchestration, or rollout failed."""


class SkaalHookError(SkaalDeployError):
    """A pre-deploy or post-deploy hook failed."""


class SkaalPluginError(SkaalError):
    """A plugin registered via entry_points could not be loaded."""


__all__ = [
    "SkaalBackendError",
    "SkaalConflict",
    "SkaalDeployError",
    "SkaalError",
    "SkaalHookError",
    "SkaalPluginError",
    "SkaalUnavailable",
]
