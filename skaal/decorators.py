"""Core user-facing decorators: @storage, @compute, @scale, @handler, @shared."""

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
        _rl: Latency | None
        if isinstance(read_latency, str):
            _rl = Latency(read_latency)
        else:
            _rl = read_latency

        _wl: Latency | None
        if isinstance(write_latency, str):
            _wl = Latency(write_latency)
        else:
            _wl = write_latency

        # Collect schema hints from Store subclasses
        try:
            from skaal.storage import _schema_hints

            schema = _schema_hints(cls)
        except Exception:  # noqa: BLE001
            schema = {}

        setattr(
            cls,
            "__skaal_storage__",
            {
                "read_latency": _rl,
                "write_latency": _wl,
                "durability": Durability(durability) if isinstance(durability, str) else durability,
                "size_hint": size_hint,
                "access_pattern": (
                    AccessPattern(access_pattern)
                    if isinstance(access_pattern, str)
                    else access_pattern
                ),
                "write_throughput": (
                    Throughput(write_throughput)
                    if isinstance(write_throughput, str)
                    else write_throughput
                ),
                "residency": residency,
                "retention": retention,
                "auto_optimize": auto_optimize,
                "decommission_policy": decommission_policy,
                "collocate_with": collocate_with,
                "schema": schema,  # empty dict for plain classes
            },
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
