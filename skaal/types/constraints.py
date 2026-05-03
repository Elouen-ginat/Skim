"""Constraint value types: Latency, Throughput, Durability, AccessPattern, etc."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class _StrictStrEnum(str, Enum):
    """str-Enum with a "did you mean…" hook on invalid lookups."""

    @classmethod
    def _missing_(cls, value: Any) -> None:
        valid = [m.value for m in cls]  # type: ignore[var-annotated]
        suggestions = difflib.get_close_matches(str(value), valid, n=1)
        hint = f" Did you mean {suggestions[0]!r}?" if suggestions else ""
        raise ValueError(
            f"{value!r} is not a valid {cls.__name__}.{hint} "
            f"Valid values: {', '.join(repr(v) for v in valid)}."
        )


class Durability(_StrictStrEnum):
    """Storage durability tier."""

    EPHEMERAL = "ephemeral"  # in-memory only, lost on restart
    PERSISTENT = "persistent"  # survives restarts (disk-backed)
    DURABLE = "durable"  # replicated, high-availability (e.g. S3 11-nines)


class AccessPattern(_StrictStrEnum):
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
    WORM = "worm"  # write-once-read-many (audit logs, compliance)


class Consistency(_StrictStrEnum):
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


class Persistent(Generic[T]):
    """
    Type annotation marker for Agent fields that should persist across restarts.

    Usage in Agent subclasses::

        from skaal.types import Persistent

        class MyAgent(Agent):
            # Field will be persisted
            score: Persistent[float] = 0.0

            # Field will NOT be persisted (no Persistent wrapper)
            _internal_state: dict = {}

    This enables explicit opt-in for persistent fields rather than marking
    all non-underscore fields as persistent by default.

    Detection in :class:`~skaal.agent.Agent.__init_subclass__`:

    - ``Persistent[float]`` → ``__origin__`` is ``Persistent`` (set by Generic)
    - bare ``Persistent`` → matched via ``annotation is Persistent``
    """
