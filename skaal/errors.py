"""Skaal exception hierarchy.

Every backend, plugin, or deploy path that raises an error should surface it
as a subclass of :class:`SkaalError` so callers can write portable
``except`` clauses regardless of which concrete backend is active.

Native exceptions from underlying libraries (``aioredis.WatchError``,
``asyncpg.UniqueViolationError``, ``botocore.exceptions.ClientError``, …)
are wrapped at the backend boundary — never leaked through the protocol.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")


class SkaalError(Exception):
    """Base class for every Skaal-originating exception."""

    exit_code: int = 1


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


class PlanError(SkaalError):
    """Plan generation failed."""


class BuildError(SkaalError):
    """Artifact generation failed."""


# ── Config / catalog / solver errors ──────────────────────────────────────────


class SkaalConfigError(SkaalError):
    """Configuration (catalog, settings, pyproject) is invalid or unreadable."""


class CatalogError(SkaalConfigError):
    """Catalog resolution or validation failed."""


class SkaalSolverError(SkaalError):
    """Constraint solving failed.

    Common parent of :class:`UnsatisfiableConstraints` so the CLI can install
    a single error-boundary branch for every solver-side problem.
    """

    exit_code: int = 2


class UnsatisfiableConstraints(SkaalSolverError):
    """No catalog entry satisfies the declared constraints.

    Carries an optional :class:`~skaal.types.solver.Diagnosis` describing
    which candidates were considered and which constraint each one
    violated.  ``diagnosis is None`` corresponds to the legacy short-string
    error path — preserved for backwards compatibility.
    """

    def __init__(
        self,
        resource_name: str,
        reason: str = "",
        *,
        diagnosis: Any = None,
    ) -> None:
        self.resource_name = resource_name
        self.reason = reason
        self.diagnosis = diagnosis
        super().__init__(f"Cannot satisfy constraints for {resource_name!r}. {reason}".rstrip())

    @property
    def variable_name(self) -> str:
        """Back-compat alias for the storage-specific name used pre-ADR 021."""
        return self.resource_name

    @property
    def function_name(self) -> str:
        """Back-compat alias for the compute-specific name used pre-ADR 021."""
        return self.resource_name


# ── Optional-extra import wrapping ────────────────────────────────────────────


class SecretMissingError(SkaalConfigError):
    """A required secret could not be resolved at runtime warmup.

    Carries the secret ``name`` and ``provider`` so the operator knows which
    declaration to fix.  Raised by :meth:`SecretRegistry.warmup` when a
    secret declared with ``required=True`` resolves to ``None``.
    """

    def __init__(self, name: str, provider: str, *, detail: str | None = None) -> None:
        self.name = name
        self.provider = provider
        message = f"Required secret {name!r} not found via provider {provider!r}"
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)


class MissingExtraError(SkaalError):
    """An optional dependency group is not installed.

    Raised by :func:`require_extra` when a feature gated behind a
    ``pip install 'skaal[<name>]'`` extra is reached without the
    corresponding packages on ``sys.path``.
    """


def require_extra(
    extra: str,
    modules: Iterable[str],
    *,
    feature: str | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator that turns a missing optional dep into a :class:`MissingExtraError`.

    Args:
        extra:    The extra name as it appears in ``pip install 'skaal[X]'``.
        modules:  Top-level modules whose presence proves the extra is installed.
                  The first ``ImportError`` is converted to ``MissingExtraError``.
        feature:  Human-readable feature name for the error message. Defaults
                  to ``extra``.

    Example::

        @require_extra("vector", ["langchain_core"], feature="vector storage")
        def _build_vector_index(...): ...
    """
    feature_name = feature or extra
    module_list = list(modules)

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            for mod in module_list:
                try:
                    __import__(mod)
                except ImportError as exc:
                    raise MissingExtraError(
                        f"{feature_name} requires the {extra!r} extra. "
                        f"Install it with `pip install 'skaal[{extra}]'`."
                    ) from exc
            return func(*args, **kwargs)

        return wrapper

    return decorator


__all__ = [
    "BuildError",
    "CatalogError",
    "MissingExtraError",
    "PlanError",
    "SecretMissingError",
    "SkaalBackendError",
    "SkaalConfigError",
    "SkaalConflict",
    "SkaalDeployError",
    "SkaalError",
    "SkaalHookError",
    "SkaalPluginError",
    "SkaalSolverError",
    "SkaalUnavailable",
    "UnsatisfiableConstraints",
    "require_extra",
]
