"""Core user-facing decorators: @storage, @compute, @scale, @deploy, @handler, @shared."""

from __future__ import annotations

import functools
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

F = TypeVar("F", bound=Callable[..., Any])


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
) -> Callable[[type], type]:
    """Declare infrastructure constraints for a storage variable or Map class."""

    def decorator(cls: type) -> type:
        if isinstance(read_latency, str):
            _rl = Latency(read_latency)
        else:
            _rl = read_latency

        if isinstance(write_latency, str):
            _wl = Latency(write_latency)
        else:
            _wl = write_latency

        cls.__skim_storage__ = {
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
                Throughput(write_throughput) if isinstance(write_throughput, str) else write_throughput
            ),
            "residency": residency,
            "retention": retention,
            "auto_optimize": auto_optimize,
            "decommission_policy": decommission_policy,
            "collocate_with": collocate_with,
        }
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
) -> Callable[[F], F]:
    """Declare infrastructure constraints for a compute function."""

    def decorator(fn: F) -> F:
        fn.__skim_compute__ = Compute(  # type: ignore[attr-defined]
            latency=latency,
            throughput=throughput,
            compute_type=compute_type,
            memory=memory,
            schedule=schedule,
        )
        fn.__skim_compute__.collocate_with = collocate_with  # type: ignore[attr-defined]
        return fn

    return decorator


def scale(
    *,
    instances: int | str = "auto",
    strategy: ScaleStrategy | str = ScaleStrategy.ROUND_ROBIN,
) -> Callable[[F], F]:
    """Declare scaling policy for a function."""

    def decorator(fn: F) -> F:
        fn.__skim_scale__ = Scale(instances=instances, strategy=strategy)  # type: ignore[attr-defined]
        return fn

    return decorator


def deploy(
    *,
    target: str = "k8s",
    region: str | None = None,
    min_instances: int = 1,
    max_instances: int = 10,
    scale_on: str | None = None,
    overflow: str | None = None,
) -> Callable[[F], F]:
    """Declare deployment target for the application entry point."""

    def decorator(fn: F) -> F:
        fn.__skim_deploy__ = {  # type: ignore[attr-defined]
            "target": target,
            "region": region,
            "min_instances": min_instances,
            "max_instances": max_instances,
            "scale_on": scale_on,
            "overflow": overflow,
        }
        return fn

    return decorator


def shared(
    *,
    consistency: Consistency | str = Consistency.EVENTUAL,
) -> Callable[[F], F]:
    """Mark a variable or Channel as distributed across all instances."""

    def decorator(fn: F) -> F:
        fn.__skim_shared__ = {  # type: ignore[attr-defined]
            "consistency": (
                Consistency(consistency) if isinstance(consistency, str) else consistency
            ),
        }
        return fn

    return decorator


def handler(fn: F) -> F:
    """Mark a method on an Agent as a message handler."""
    fn.__skim_handler__ = True  # type: ignore[attr-defined]
    return fn
