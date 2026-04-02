"""Constraint value types: Latency, Throughput, Durability, AccessPattern, etc."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Durability(str, Enum):
    """Storage durability tier."""
    EPHEMERAL = "ephemeral"    # in-memory only, lost on restart
    PERSISTENT = "persistent"  # survives restarts (disk-backed)
    DURABLE = "durable"        # replicated, high-availability (e.g. S3 11-nines)


class AccessPattern(str, Enum):
    """How data is primarily accessed; guides backend selection."""
    RANDOM_READ = "random-read"
    RANDOM_WRITE = "random-write"
    SEQUENTIAL = "sequential"
    APPEND_ONLY = "append-only"
    WRITE_HEAVY = "write-heavy"
    BULK_READ = "bulk-read"
    TRANSACTIONAL = "transactional"
    PUB_SUB = "pub-sub"
    EVENT_LOG = "event-log"  # immutable, ordered, replayable → Kafka/Kinesis
    WORM = "worm"            # write-once-read-many (audit logs, compliance)


class Consistency(str, Enum):
    """Read consistency model for shared/distributed state."""
    EVENTUAL = "eventual"
    STRONG = "strong"
    CAUSAL = "causal"


@dataclass
class Latency:
    """Parsed latency constraint, e.g. ``Latency('< 5ms')``."""

    expr: str
    ms: float
    op: str  # '<', '<=', '>', '>='

    def __init__(self, expr: str) -> None:
        self.expr = expr
        m = re.match(r"([<>]=?)\s*([\d.]+)\s*ms", expr.strip())
        if not m:
            raise ValueError(f"Invalid latency expression: {expr!r}. Expected e.g. '< 5ms'")
        self.op = m.group(1)
        self.ms = float(m.group(2))

    def __repr__(self) -> str:
        return f"Latency({self.expr!r})"


@dataclass
class Throughput:
    """Parsed throughput constraint, e.g. ``Throughput('> 1000 req/s')``."""

    expr: str
    value: float
    unit: str  # 'req/s', 'MB/s', 'events/s'
    op: str

    def __init__(self, expr: str) -> None:
        self.expr = expr
        m = re.match(r"([<>]=?)\s*([\d.]+)\s*(.+)", expr.strip())
        if not m:
            raise ValueError(f"Invalid throughput expression: {expr!r}")
        self.op = m.group(1)
        self.value = float(m.group(2))
        self.unit = m.group(3).strip()

    def __repr__(self) -> str:
        return f"Throughput({self.expr!r})"


@dataclass
class DecommissionPolicy:
    """Policy for decommissioning old infrastructure after a completed migration."""
    retention_days: int = 30
    archive: bool = True
    archive_target: str = "s3"
