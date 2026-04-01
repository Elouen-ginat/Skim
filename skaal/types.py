"""Constraint type primitives used in Skim decorator annotations."""

from __future__ import annotations

import re
from enum import Enum
from dataclasses import dataclass, field
from typing import Literal


class Durability(str, Enum):
    EPHEMERAL = "ephemeral"
    PERSISTENT = "persistent"
    DURABLE = "durable"


class AccessPattern(str, Enum):
    RANDOM_READ = "random-read"
    RANDOM_WRITE = "random-write"
    SEQUENTIAL = "sequential"
    APPEND_ONLY = "append-only"
    WRITE_HEAVY = "write-heavy"
    BULK_READ = "bulk-read"
    TRANSACTIONAL = "transactional"
    PUB_SUB = "pub-sub"
    EVENT_LOG = "event-log"   # immutable, ordered, replayable — solver maps to Kafka/Kinesis
    WORM = "worm"             # write-once-read-many (audit logs, compliance archives)


class ComputeType(str, Enum):
    CPU = "cpu"
    GPU = "gpu"
    TPU = "tpu"
    ANY = "any"


class ScaleStrategy(str, Enum):
    ROUND_ROBIN = "round-robin"
    PARTITION_BY_KEY = "partition-by-key"
    BROADCAST = "broadcast"
    RACE = "race"
    COMPETING_CONSUMER = "competing-consumer"


class Consistency(str, Enum):
    EVENTUAL = "eventual"
    STRONG = "strong"
    CAUSAL = "causal"


@dataclass
class Latency:
    """Represents a latency constraint, e.g. Latency('< 5ms')."""

    expr: str
    ms: float
    op: str  # '<', '<=', '>', '>='

    def __init__(self, expr: str) -> None:
        self.expr = expr
        match = re.match(r"([<>]=?)\s*([\d.]+)\s*ms", expr.strip())
        if not match:
            raise ValueError(f"Invalid latency expression: {expr!r}. Expected e.g. '< 5ms'")
        self.op = match.group(1)
        self.ms = float(match.group(2))

    def __repr__(self) -> str:
        return f"Latency({self.expr!r})"


@dataclass
class Throughput:
    """Represents a throughput constraint, e.g. Throughput('> 1000 req/s')."""

    expr: str
    value: float
    unit: str  # 'req/s', 'MB/s', 'events/s'
    op: str

    def __init__(self, expr: str) -> None:
        self.expr = expr
        match = re.match(r"([<>]=?)\s*([\d.]+)\s*(.+)", expr.strip())
        if not match:
            raise ValueError(f"Invalid throughput expression: {expr!r}")
        self.op = match.group(1)
        self.value = float(match.group(2))
        self.unit = match.group(3).strip()

    def __repr__(self) -> str:
        return f"Throughput({self.expr!r})"


@dataclass
class Scale:
    """Compute scaling parameters."""

    instances: int | str = "auto"  # int or "auto"
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
    idempotency_key: str | None = None  # name of the function arg carrying the key


@dataclass
class CircuitBreaker:
    """Open the circuit after N failures; probe again after recovery_timeout_ms."""

    failure_threshold: int = 5
    recovery_timeout_ms: int = 10_000
    fallback: str | None = None  # name of a registered fallback function


@dataclass
class RateLimitPolicy:
    """Rate limiting, optionally scoped per-client or per-key argument."""

    requests_per_second: float
    burst: int = 1
    # "global" | "per-client" | "per-key:<arg_name>"
    # e.g. scope="per-key:customer_id" limits per unique customer_id value
    scope: str = "global"


@dataclass
class Bulkhead:
    """Limit concurrent calls into a function; callers block up to max_wait_ms."""

    max_concurrent_calls: int
    max_wait_ms: int = 0


@dataclass
class Compute:
    """Compute constraint parameters."""

    latency: Latency | str | None = None
    throughput: Throughput | str | None = None
    compute_type: ComputeType = ComputeType.CPU
    memory: str | None = None  # e.g. "~ 2GB"
    schedule: str = "realtime"  # "realtime", "batch", "streaming"
    retry: RetryPolicy | None = None
    circuit_breaker: CircuitBreaker | None = None
    rate_limit: RateLimitPolicy | None = None
    bulkhead: Bulkhead | None = None
    collocate_with: str | None = None   # qualified resource name: "auth.Sessions"

    def __post_init__(self) -> None:
        if isinstance(self.latency, str):
            self.latency = Latency(self.latency)
        if isinstance(self.throughput, str):
            self.throughput = Throughput(self.throughput)
        if isinstance(self.compute_type, str):
            self.compute_type = ComputeType(self.compute_type)


@dataclass
class DecommissionPolicy:
    """Policy for decommissioning old infrastructure after migration."""

    retention_days: int = 30
    archive: bool = True
    archive_target: str = "s3"
