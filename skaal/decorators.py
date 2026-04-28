"""Core user-facing decorators: @storage, @relational, @vector, @compute, @scale, @handler, @shared."""

from __future__ import annotations

from typing import Any, Callable, TypeVar

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
    Throughput,
)
from skaal.types.compute import Bulkhead, CircuitBreaker, RateLimitPolicy, RetryPolicy

F = TypeVar("F", bound=Callable[..., Any])
C = TypeVar("C", bound=type)


def _build_storage_attrs(
    *,
    kind: str | None,
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
) -> dict[str, Any]:
    """Normalise the constraint kwargs shared by ``@storage``, ``@relational``,
    and ``@vector`` into the dict stored on ``__skaal_storage__``."""
    attrs: dict[str, Any] = {
        "read_latency": Latency(read_latency) if isinstance(read_latency, str) else read_latency,
        "write_latency": (
            Latency(write_latency) if isinstance(write_latency, str) else write_latency
        ),
        "durability": Durability(durability) if isinstance(durability, str) else durability,
        "size_hint": size_hint,
        "access_pattern": (
            AccessPattern(access_pattern) if isinstance(access_pattern, str) else access_pattern
        ),
        "write_throughput": (
            Throughput(write_throughput) if isinstance(write_throughput, str) else write_throughput
        ),
        "residency": residency,
        "retention": retention,
        "auto_optimize": auto_optimize,
        "decommission_policy": decommission_policy,
        "collocate_with": collocate_with,
        "schema": schema,
    }
    if kind is not None:
        attrs["kind"] = kind
    return attrs


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
) -> Callable[[C], C]:
    """Declare infrastructure constraints for a storage variable or Store class."""

    def decorator(cls: C) -> C:
        try:
            from skaal.storage import _schema_hints

            schema = _schema_hints(cls)
        except Exception:  # noqa: BLE001
            schema = {}

        setattr(
            cls,
            "__skaal_storage__",
            _build_storage_attrs(
                kind=None,
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
                schema=schema,
            ),
        )
        return cls

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

        setattr(
            cls,
            "__skaal_storage__",
            _build_storage_attrs(
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
                schema=_schema_hints(cls),
            ),
        )
        return cls

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

        if dim <= 0:
            raise ValueError("@app.vector requires dim > 0.")

        setattr(cls, "__skaal_vector_dimensions__", dim)
        setattr(cls, "__skaal_vector_metric__", metric.lower())

        setattr(
            cls,
            "__skaal_storage__",
            _build_storage_attrs(
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
                schema=_schema_hints(cls),
            ),
        )
        return cls

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
        setattr(
            fn,
            "__skaal_compute__",
            Compute(
                latency=latency,
                throughput=throughput,
                compute_type=ComputeType(compute_type)
                if isinstance(compute_type, str)
                else compute_type,
                memory=memory,
                schedule=schedule,
                collocate_with=collocate_with,
                retry=retry,
                circuit_breaker=circuit_breaker,
                rate_limit=rate_limit,
                bulkhead=bulkhead,
            ),
        )
        return fn

    return decorator


def scale(
    *,
    instances: int | str = "auto",
    strategy: ScaleStrategy | str = ScaleStrategy.ROUND_ROBIN,
) -> Callable[[F], F]:
    """Declare scaling policy for a function."""

    def decorator(fn: F) -> F:
        setattr(
            fn,
            "__skaal_scale__",
            Scale(
                instances=instances,
                strategy=ScaleStrategy(strategy) if isinstance(strategy, str) else strategy,
            ),
        )
        return fn

    return decorator


def shared(
    *,
    consistency: Consistency | str = Consistency.EVENTUAL,
) -> Callable[[F], F]:
    """Mark a variable or Channel as distributed across all instances."""

    def decorator(fn: F) -> F:
        setattr(
            fn,
            "__skaal_shared__",
            {
                "consistency": (
                    Consistency(consistency) if isinstance(consistency, str) else consistency
                ),
            },
        )
        return fn

    return decorator


def handler(fn: F) -> F:
    """Mark a method on an Agent as a message handler."""
    setattr(fn, "__skaal_handler__", True)
    return fn
