"""Compute constraint types: ComputeType, Scale, Compute, and resilience policies."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

from skaal.types.constraints import Latency, Throughput


class ComputeType(str, Enum):
    """Hardware type required by a function."""

    CPU = "cpu"
    GPU = "gpu"
    TPU = "tpu"
    ANY = "any"


class ScaleStrategy(str, Enum):
    """How requests are distributed across instances."""

    ROUND_ROBIN = "round-robin"
    PARTITION_BY_KEY = "partition-by-key"
    BROADCAST = "broadcast"
    RACE = "race"
    COMPETING_CONSUMER = "competing-consumer"


@dataclass
class Scale:
    """Compute scaling parameters."""

    instances: int | str = "auto"
    strategy: ScaleStrategy = ScaleStrategy.ROUND_ROBIN

    def __post_init__(self) -> None:
        if isinstance(self.strategy, str):
            self.strategy = ScaleStrategy(self.strategy)


@dataclass
class RetryPolicy:
    """Retry-with-backoff and optional idempotency for a function."""

    max_attempts: int = 3
    backoff: Literal["fixed", "linear", "exponential"] = "exponential"
    base_delay_ms: int = 100
    max_delay_ms: int = 30_000
    idempotency_key: str | None = None


@dataclass
class CircuitBreaker:
    """Open the circuit after N consecutive failures; probe after recovery_timeout_ms."""

    failure_threshold: int = 5
    recovery_timeout_ms: int = 10_000
    fallback: str | None = None  # name of a registered @app.function


@dataclass
class RateLimitPolicy:
    """Token-bucket rate limiting, optionally scoped per-client or per-argument."""

    requests_per_second: float
    burst: int = 1
    scope: str = "global"  # "global" | "per-client" | "per-key:<arg_name>"


@dataclass
class Bulkhead:
    """Limit concurrent calls; callers block up to max_wait_ms then fail fast."""

    max_concurrent_calls: int
    max_wait_ms: int = 0


@dataclass
class Compute:
    """Full compute constraint specification attached to ``@app.function()``."""

    latency: Latency | str | None = None
    throughput: Throughput | str | None = None
    compute_type: ComputeType = ComputeType.CPU
    memory: str | None = None  # e.g. "~ 2GB"
    schedule: str = "realtime"  # "realtime" | "batch" | "streaming"
    retry: RetryPolicy | None = None
    circuit_breaker: CircuitBreaker | None = None
    rate_limit: RateLimitPolicy | None = None
    bulkhead: Bulkhead | None = None
    collocate_with: str | None = None  # qualified resource name: "auth.Sessions"

    def __post_init__(self) -> None:
        if isinstance(self.latency, str):
            self.latency = Latency(self.latency)
        if isinstance(self.throughput, str):
            self.throughput = Throughput(self.throughput)
        if isinstance(self.compute_type, str):
            self.compute_type = ComputeType(self.compute_type)
