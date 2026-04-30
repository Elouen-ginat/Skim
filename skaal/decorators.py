"""Core user-facing decorators: @storage, @relational, @vector, @compute, @scale, @handler, @shared."""

from __future__ import annotations

from enum import Enum
from typing import Any, Callable, TypeVar, cast

from skaal.blob import BlobStore, validate_blob_model
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


def storage(
    *,
    read_latency: Latency | str | None = None,
    write_latency: Latency | str | None = None,
    durability: Durability | str = Durability.PERSISTENT,
    size_hint: str | None = None,
    access_pattern: AccessPattern | str = AccessPattern.RANDOM_READ,
    write_throughput: Throughput | str | None = None,
    residency: str | None = None,
    retention: str | None = None,
    auto_optimize: bool = False,
    decommission_policy: DecommissionPolicy | None = None,
    collocate_with: str | None = None,
    indexes: list[SecondaryIndex] | None = None,
) -> Callable[[C], C]:
    """Declare infrastructure constraints for a storage variable or Store class."""

    def decorator(cls: C) -> C:
        # Collect schema hints from Store subclasses
        try:
            from skaal.storage import _schema_hints

            schema = _schema_hints(cls)
        except Exception:  # noqa: BLE001
            schema = {}

        return _apply_metadata(
            cls,
            "__skaal_storage__",
            _build_storage_metadata(
                kind="kv",
                read_latency=read_latency,
                write_latency=write_latency,
                durability=durability,
                size_hint=size_hint,
                access_pattern=access_pattern,
                write_throughput=write_throughput,
                residency=residency,
                retention=retention,
                auto_optimize=auto_optimize,
                decommission_policy=decommission_policy,
                collocate_with=collocate_with,
                indexes=indexes,
                schema=schema,
            ),
        )

    return decorator


def blob(
    *,
    read_latency: Latency | str | None = None,
    write_latency: Latency | str | None = None,
    durability: Durability | str = Durability.PERSISTENT,
    size_hint: str | None = None,
    access_pattern: AccessPattern | str = AccessPattern.BULK_READ,
    write_throughput: Throughput | str | None = None,
    residency: str | None = None,
    retention: str | None = None,
    auto_optimize: bool = False,
    decommission_policy: DecommissionPolicy | None = None,
    collocate_with: str | None = None,
) -> Callable[[C], C]:
    """Declare infrastructure constraints for a blob storage class."""

    outer = storage(
        read_latency=read_latency,
        write_latency=write_latency,
        durability=durability,
        size_hint=size_hint,
        access_pattern=access_pattern,
        write_throughput=write_throughput,
        residency=residency,
        retention=retention,
        auto_optimize=auto_optimize,
        decommission_policy=decommission_policy,
        collocate_with=collocate_with,
    )

    def decorator(cls: C) -> C:
        if not isinstance(cls, type) or not issubclass(cls, BlobStore):
            raise TypeError("@app.blob requires a skaal.BlobStore subclass.")
        validate_blob_model(cls)
        annotated = outer(cls)
        getattr(annotated, "__skaal_storage__", {})["kind"] = "blob"
        return cast(C, annotated)

    return decorator


def relational(
    *,
    read_latency: Latency | str | None = None,
    write_latency: Latency | str | None = None,
    durability: Durability | str = Durability.PERSISTENT,
    size_hint: str | None = None,
    write_throughput: Throughput | str | None = None,
    residency: str | None = None,
    auto_optimize: bool = False,
    decommission_policy: DecommissionPolicy | None = None,
    collocate_with: str | None = None,
) -> Callable[[C], C]:
    """Declare infrastructure constraints for a SQLModel relational table."""

    def decorator(cls: C) -> C:
        from skaal.relational import _schema_hints, validate_relational_model

        validate_relational_model(cls)
        schema = _schema_hints(cls)

        return _apply_metadata(
            cls,
            "__skaal_storage__",
            _build_storage_metadata(
                kind="relational",
                read_latency=read_latency,
                write_latency=write_latency,
                durability=durability,
                size_hint=size_hint,
                access_pattern=AccessPattern.TRANSACTIONAL,
                write_throughput=write_throughput,
                residency=residency,
                retention=None,
                auto_optimize=auto_optimize,
                decommission_policy=decommission_policy,
                collocate_with=collocate_with,
                schema=schema,
            ),
        )

    return decorator


def vector(
    *,
    dim: int,
    metric: str = "cosine",
    read_latency: Latency | str | None = None,
    write_latency: Latency | str | None = None,
    durability: Durability | str = Durability.PERSISTENT,
    size_hint: str | None = None,
    write_throughput: Throughput | str | None = None,
    residency: str | None = None,
    auto_optimize: bool = False,
    decommission_policy: DecommissionPolicy | None = None,
    collocate_with: str | None = None,
) -> Callable[[C], C]:
    """Declare infrastructure constraints for a typed vector store."""

    def decorator(cls: C) -> C:
        from skaal.vector import _schema_hints, validate_vector_model

        validate_vector_model(cls)

        normalized_metric = metric.lower()
        if dim <= 0:
            raise ValueError("@app.vector requires dim > 0.")

        setattr(cls, "__skaal_vector_dimensions__", dim)
        setattr(cls, "__skaal_vector_metric__", normalized_metric)
        schema = _schema_hints(cls)

        return _apply_metadata(
            cls,
            "__skaal_storage__",
            _build_storage_metadata(
                kind="vector",
                read_latency=read_latency,
                write_latency=write_latency,
                durability=durability,
                size_hint=size_hint,
                access_pattern=AccessPattern.BULK_READ,
                write_throughput=write_throughput,
                residency=residency,
                retention=None,
                auto_optimize=auto_optimize,
                decommission_policy=decommission_policy,
                collocate_with=collocate_with,
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
