"""Core user-facing decorators: @storage, @compute, @scale, @handler, @shared."""

from __future__ import annotations

from enum import Enum
from typing import Any, Callable, Literal, TypeVar, cast, overload

from skaal.blob import validate_blob_model
from skaal.types import (
    AccessPattern,
    Compute,
    ComputeType,
    Consistency,
    DecommissionPolicy,
    Durability,
    Latency,
    Scale,
    ScaleStrategy,
    SecondaryIndex,
    Throughput,
)
from skaal.types.compute import Bulkhead, CircuitBreaker, RateLimitPolicy, RetryPolicy

F = TypeVar("F", bound=Callable[..., Any])
C = TypeVar("C", bound=type)
E = TypeVar("E", bound=Enum)
StorageKind = Literal["kv", "blob", "relational", "vector"]


def _coerce_enum(value: E | str | None, enum_type: type[E]) -> E | None:
    if value is None or isinstance(value, enum_type):
        return value
    return enum_type(value)


def _apply_metadata(target: C, attribute: str, metadata: Any) -> C:
    setattr(target, attribute, metadata)
    return target


def _apply_callable_metadata(target: F, attribute: str, metadata: Any) -> F:
    setattr(target, attribute, metadata)
    return target


def _build_storage_metadata(
    *,
    kind: str,
    read_latency: Latency | str | None,
    write_latency: Latency | str | None,
    durability: Durability | str,
    size_hint: str | None,
    access_pattern: AccessPattern | str,
    write_throughput: Throughput | str | None,
    residency: str | None,
    retention: str | None,
    auto_optimize: bool,
    decommission_policy: DecommissionPolicy | None,
    collocate_with: str | None,
    schema: dict[str, Any],
    indexes: list[SecondaryIndex] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "read_latency": _coerce_enum(read_latency, Latency),
        "write_latency": _coerce_enum(write_latency, Latency),
        "durability": _coerce_enum(durability, Durability),
        "size_hint": size_hint,
        "access_pattern": _coerce_enum(access_pattern, AccessPattern),
        "write_throughput": _coerce_enum(write_throughput, Throughput),
        "residency": residency,
        "retention": retention,
        "auto_optimize": auto_optimize,
        "decommission_policy": decommission_policy,
        "collocate_with": collocate_with,
        "indexes": list(indexes or []),
        "schema": schema,
    }


def _normalize_storage_kind(kind: StorageKind | str) -> StorageKind:
    normalized = kind.strip().lower()
    if normalized not in {"kv", "blob", "relational", "vector"}:
        raise ValueError(f"Unsupported storage kind: {kind!r}")
    return cast(StorageKind, normalized)


def _default_access_pattern(kind: StorageKind) -> AccessPattern:
    if kind == "blob":
        return AccessPattern.BULK_READ
    if kind == "relational":
        return AccessPattern.TRANSACTIONAL
    if kind == "vector":
        return AccessPattern.BULK_READ
    return AccessPattern.RANDOM_READ


def _storage_schema(
    cls: C,
    *,
    kind: StorageKind,
    dim: int | None,
    metric: str,
) -> dict[str, Any]:
    if kind == "blob":
        validate_blob_model(cls)
        try:
            from skaal.storage import _schema_hints

            return _schema_hints(cls)
        except Exception:  # noqa: BLE001
            return {}

    if kind == "relational":
        from skaal.relational import _schema_hints as relational_schema_hints
        from skaal.relational import validate_relational_model

        validate_relational_model(cls)
        return relational_schema_hints(cls)

    if kind == "vector":
        from skaal.vector import _schema_hints as vector_schema_hints
        from skaal.vector import validate_vector_model

        validate_vector_model(cls)
        if dim is None or dim <= 0:
            raise ValueError('@app.storage(kind="vector") requires dim > 0.')
        setattr(cls, "__skaal_vector_dimensions__", dim)
        setattr(cls, "__skaal_vector_metric__", metric.lower())
        return vector_schema_hints(cls)

    try:
        from skaal.storage import _schema_hints

        return _schema_hints(cls)
    except Exception:  # noqa: BLE001
        return {}


@overload
def storage(
    *,
    kind: Literal["vector"],
    dim: int,
    metric: str = "cosine",
    read_latency: Latency | str | None = None,
    write_latency: Latency | str | None = None,
    durability: Durability | str = Durability.PERSISTENT,
    size_hint: str | None = None,
    access_pattern: AccessPattern | str | None = None,
    write_throughput: Throughput | str | None = None,
    residency: str | None = None,
    retention: str | None = None,
    auto_optimize: bool = False,
    decommission_policy: DecommissionPolicy | None = None,
    collocate_with: str | None = None,
    indexes: list[SecondaryIndex] | None = None,
) -> Callable[[C], C]: ...


@overload
def storage(
    *,
    kind: StorageKind | str = "kv",
    dim: None = None,
    metric: str = "cosine",
    read_latency: Latency | str | None = None,
    write_latency: Latency | str | None = None,
    durability: Durability | str = Durability.PERSISTENT,
    size_hint: str | None = None,
    access_pattern: AccessPattern | str | None = None,
    write_throughput: Throughput | str | None = None,
    residency: str | None = None,
    retention: str | None = None,
    auto_optimize: bool = False,
    decommission_policy: DecommissionPolicy | None = None,
    collocate_with: str | None = None,
    indexes: list[SecondaryIndex] | None = None,
) -> Callable[[C], C]: ...


def storage(
    *,
    kind: StorageKind | str = "kv",
    dim: int | None = None,
    metric: str = "cosine",
    read_latency: Latency | str | None = None,
    write_latency: Latency | str | None = None,
    durability: Durability | str = Durability.PERSISTENT,
    size_hint: str | None = None,
    access_pattern: AccessPattern | str | None = None,
    write_throughput: Throughput | str | None = None,
    residency: str | None = None,
    retention: str | None = None,
    auto_optimize: bool = False,
    decommission_policy: DecommissionPolicy | None = None,
    collocate_with: str | None = None,
    indexes: list[SecondaryIndex] | None = None,
) -> Callable[[C], C]:
    """Declare infrastructure constraints for a storage variable or Store class."""
    normalized_kind = _normalize_storage_kind(kind)

    def decorator(cls: C) -> C:
        schema = _storage_schema(cls, kind=normalized_kind, dim=dim, metric=metric)

        return _apply_metadata(
            cls,
            "__skaal_storage__",
            _build_storage_metadata(
                kind=normalized_kind,
                read_latency=read_latency,
                write_latency=write_latency,
                durability=durability,
                size_hint=size_hint,
                access_pattern=access_pattern or _default_access_pattern(normalized_kind),
                write_throughput=write_throughput,
                residency=residency,
                retention=retention if normalized_kind in {"kv", "blob"} else None,
                auto_optimize=auto_optimize,
                decommission_policy=decommission_policy,
                collocate_with=collocate_with,
                indexes=indexes if normalized_kind == "kv" else None,
                schema=schema,
            ),
        )

    return decorator


def compute(
    *,
    latency: Latency | str | None = None,
    throughput: Throughput | str | None = None,
    compute_type: ComputeType | str = ComputeType.CPU,
    memory: str | None = None,
    schedule: str = "realtime",
    collocate_with: str | None = None,
    retry: RetryPolicy | None = None,
    circuit_breaker: CircuitBreaker | None = None,
    rate_limit: RateLimitPolicy | None = None,
    bulkhead: Bulkhead | None = None,
) -> Callable[[F], F]:
    """Declare infrastructure constraints for a compute function.

    Resilience policies (*retry*, *circuit_breaker*, *rate_limit*, *bulkhead*)
    are honoured by the runtime — see :mod:`skaal.runtime.middleware`.
    """

    def decorator(fn: F) -> F:
        return _apply_callable_metadata(
            fn,
            "__skaal_compute__",
            Compute(
                latency=latency,
                throughput=throughput,
                compute_type=_coerce_enum(compute_type, ComputeType) or ComputeType.CPU,
                memory=memory,
                schedule=schedule,
                collocate_with=collocate_with,
                retry=retry,
                circuit_breaker=circuit_breaker,
                rate_limit=rate_limit,
                bulkhead=bulkhead,
            ),
        )

    return decorator


def scale(
    *,
    instances: int | str = "auto",
    strategy: ScaleStrategy | str = ScaleStrategy.ROUND_ROBIN,
) -> Callable[[F], F]:
    """Declare scaling policy for a function."""

    def decorator(fn: F) -> F:
        return _apply_callable_metadata(
            fn,
            "__skaal_scale__",
            Scale(
                instances=instances,
                strategy=_coerce_enum(strategy, ScaleStrategy) or ScaleStrategy.ROUND_ROBIN,
            ),
        )

    return decorator


def shared(
    *,
    consistency: Consistency | str = Consistency.EVENTUAL,
) -> Callable[[F], F]:
    """Mark a variable or Channel as distributed across all instances."""

    def decorator(fn: F) -> F:
        return _apply_callable_metadata(
            fn,
            "__skaal_shared__",
            {
                "consistency": _coerce_enum(consistency, Consistency) or Consistency.EVENTUAL,
            },
        )

    return decorator


def handler(fn: F) -> F:
    """Mark a method on an Agent as a message handler."""
    return _apply_callable_metadata(fn, "__skaal_handler__", True)
