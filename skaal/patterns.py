"""
Skaal distributed system patterns.

Higher-level primitives that compose existing Skaal storage/channel/agent
declarations into well-known distributed system patterns.

Patterns are registered with a module via ``module.pattern(p)`` and attach
``__skaal_pattern__`` metadata consumed by the solver (mirroring the
``__skaal_storage__`` convention).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, AsyncIterator, Generic, Literal, Protocol, TypeVar, runtime_checkable

from skaal.types import Consistency, Durability, Throughput

TSource = TypeVar("TSource")
TView = TypeVar("TView")
T = TypeVar("T")


@runtime_checkable
class Pattern(Protocol):
    """Structural marker shared by all Skaal patterns."""

    __skaal_pattern__: dict[str, object]


# ── EventLog ──────────────────────────────────────────────────────────────


class EventLog(Pattern, Generic[T]):
    """
    Append-only, ordered, replayable typed event log (Event Sourcing).

    Internally sets ``__skaal_storage__`` with
    ``access_pattern=AccessPattern.EVENT_LOG`` so the solver treats it as a
    normal storage declaration and maps it to Kafka / Kinesis / EventStore.

    Usage::

        OrderEvents: EventLog[OrderEvent] = EventLog(retention="30d", partitions=16)
        app.pattern(OrderEvents)

        # Append
        await OrderEvents.append(OrderEvent(...))

        # Replay from beginning
        async for offset, event in OrderEvents.replay(from_offset=0):
            ...

        # Subscribe (live, consumer-group semantics)
        async for offset, event in OrderEvents.subscribe("projector"):
            ...
    """

    def __init__(
        self,
        backend: Any | None = None,
        *,
        retention: str = "7d",
        partitions: int = 1,
        throughput: Throughput | str | None = None,
        durability: Durability = Durability.DURABLE,
        _backend: Any | None = None,
    ) -> None:
        self.retention = retention
        self.partitions = partitions
        self.durability = durability
        if isinstance(throughput, str):
            throughput = Throughput(throughput)
        self.throughput = throughput

        # Lazily import to avoid circular imports at module level
        # Support both positional 'backend' and keyword '_backend' for backward compatibility
        actual_backend = backend if backend is not None else _backend
        if actual_backend is not None:
            self._backend = actual_backend
        else:
            from skaal.backends.kv.local_map import LocalMap

            self._backend = LocalMap()

        # Metadata consumed by solver — mirrors __skaal_storage__
        from skaal.types import AccessPattern

        self.__skaal_pattern__ = {
            "pattern_type": "event-log",
            "storage": {
                "access_pattern": AccessPattern.EVENT_LOG,
                "durability": durability,
                "retention": retention,
                "partitions": partitions,
                "throughput": throughput,
            },
        }

        # In-process fan-out: every append fires this asyncio.Event so
        # subscribers can wake immediately instead of polling.  Production
        # backends (Kafka, Redis Streams) have native notify paths and ignore
        # this field.
        self._notify: asyncio.Event = asyncio.Event()

    # ── Runtime API ──────────────────────────────────────────────────────────

    async def append(self, event: T) -> int:
        """Append *event* to the log. Returns the assigned offset (0-based)."""
        # Use atomic increment to prevent race conditions with concurrent appends
        offset = await self._backend.increment_counter("meta:next_offset", delta=1) - 1
        await self._backend.set(f"event:{offset:020d}", event)
        # Wake any in-process subscribers waiting on this log.
        self._notify.set()
        self._notify.clear()
        return offset

    async def replay(self, from_offset: int = 0) -> AsyncIterator[tuple[int, T]]:
        """Replay events starting from *from_offset*. Yields (offset, event) tuples."""
        entries = await self._backend.scan("event:")
        for key, value in sorted((e for e in entries if e[0] >= f"event:{from_offset:020d}")):
            offset = int(key.split(":")[-1])
            yield offset, value

    async def subscribe(
        self,
        consumer_group: str,
        *,
        from_beginning: bool = False,
        poll_interval: float = 0.1,
    ) -> AsyncIterator[tuple[int, T]]:
        """
        Poll-based subscription for local mode.

        Multiple subscribers in the same group share the load; different groups
        each receive all events independently.

        Production mode uses Kafka/Redis Streams instead.
        """
        raw = await self._backend.get(f"consumer:{consumer_group}:offset")
        offset = 0 if from_beginning else (int(raw) if raw is not None else 0)
        while True:
            # Scan all events with full prefix (not offset-prefixed), then filter by range
            entries = await self._backend.scan("event:")
            sorted_entries = sorted((e for e in entries if e[0] >= f"event:{offset:020d}"))
            for key, value in sorted_entries:
                current_offset = int(key.split(":")[-1])
                await self._backend.set(f"consumer:{consumer_group}:offset", current_offset + 1)
                offset = current_offset + 1
                yield current_offset, value
            if not sorted_entries:
                # Wake on the next append; fall back to polling after poll_interval
                # so replay still works against backends without the notify path.
                try:
                    await asyncio.wait_for(self._notify.wait(), timeout=poll_interval)
                except asyncio.TimeoutError:
                    pass

    def __repr__(self) -> str:
        return (
            f"EventLog(retention={self.retention!r}, partitions={self.partitions}, "
            f"durability={self.durability!r})"
        )


# ── Projection (CQRS read-model) ──────────────────────────────────────────


class Projection(Generic[TSource, TView]):
    """
    CQRS read-model derived from a source ``EventLog``.

    Declares that *target* storage is rebuilt by applying *handler* to each
    event from *source*. The solver co-locates source and target on the same
    compute/storage cluster.

    Usage::

        OrderSummaries: Projection[OrderEvent, OrderSummary] = Projection(
            source=OrderEvents,
            target=Summaries,            # an @storage-decorated class
            handler="apply_order_event", # name of a registered @app.function
        )
        app.pattern(OrderSummaries)
    """

    def __init__(
        self,
        source: EventLog[TSource],
        target: Any,
        handler: str,
        consistency: Consistency | str = Consistency.EVENTUAL,
        checkpoint_every: int = 100,
        strict: bool = False,
    ) -> None:
        self.source = source
        self.target = target
        self.handler = handler
        self.consistency = Consistency(consistency) if isinstance(consistency, str) else consistency
        self.checkpoint_every = checkpoint_every
        self.strict = strict

        self.__skaal_pattern__ = {
            "pattern_type": "projection",
            "source": source,
            "target": target,
            "handler": handler,
            "consistency": self.consistency,
            "checkpoint_every": checkpoint_every,
            "strict": strict,
        }

    def __repr__(self) -> str:
        target_name = getattr(self.target, "__name__", repr(self.target))
        return (
            f"Projection(source={self.source!r}, target={target_name!r}, "
            f"handler={self.handler!r})"
        )


# ── Saga (distributed transactions) ──────────────────────────────────────


@dataclass
class SagaStep:
    """A single step in a Saga, with its compensating action."""

    function: str  # name of a registered @app.function / @module.function
    compensate: str  # name of the compensation function (run on failure/rollback)
    timeout_ms: int | None = None


class Saga(Pattern):
    """
    Multi-step distributed transaction with compensating actions.

    Supports two coordination strategies:

    - ``"compensation"`` (default) — Saga pattern: on failure, run compensations
      in reverse order. Preferred for long-running, loosely-coupled workflows.
    - ``"2pc"`` — Two-Phase Commit: all steps prepare then commit atomically.
      Preferred when strong atomicity is required and participants support 2PC.

    Steps reference functions by **string name** (not object reference) to
    avoid circular imports across module boundaries. The solver validates that
    all named functions are registered before ``skaal plan`` completes.

    Usage::

        PlaceOrder = Saga(
            name="place_order",
            steps=[
                SagaStep("reserve_inventory", compensate="release_inventory"),
                SagaStep("charge_payment",    compensate="refund_payment"),
                SagaStep("confirm_shipment",  compensate="cancel_shipment"),
            ],
        )
        app.pattern(PlaceOrder)
    """

    def __init__(
        self,
        name: str,
        steps: list[SagaStep],
        coordination: Literal["compensation", "2pc"] = "compensation",
        timeout_ms: int | None = None,
    ) -> None:
        self.name = name
        self.steps = steps
        self.coordination = coordination
        self.timeout_ms = timeout_ms

        self.__skaal_pattern__ = {
            "pattern_type": "saga",
            "name": name,
            "steps": [
                {"function": s.function, "compensate": s.compensate, "timeout_ms": s.timeout_ms}
                for s in steps
            ],
            "coordination": coordination,
            "timeout_ms": timeout_ms,
        }

    def __repr__(self) -> str:
        return (
            f"Saga(name={self.name!r}, steps={len(self.steps)}, "
            f"coordination={self.coordination!r})"
        )


# ── Outbox (reliable event publishing) ───────────────────────────────────


class Outbox(Generic[T]):
    """
    Transactionally-coupled event publisher (Outbox pattern).

    Guarantees at-least-once (or exactly-once) delivery: the event is written
    durably in the same transaction as the surrounding storage write, then
    forwarded asynchronously to *channel*.

    This prevents the dual-write problem where a state update can succeed but
    the corresponding event publish can fail.

    Usage::

        OrderOutbox: Outbox[OrderEvent] = Outbox(
            channel=OrderEvents,
            storage=Orders,          # events written atomically with this store
            delivery="at-least-once",
        )
        app.pattern(OrderOutbox)

        # Inside an agent handler — the runtime intercepts the return value:
        @handler
        async def confirm(self) -> OrderConfirmed:
            self.status = "confirmed"
            return OrderConfirmed(id=self.id)   # published via outbox
    """

    def __init__(
        self,
        channel: Any,  # Channel[T] — typed as Any to avoid circular import
        storage: Any,  # @storage-annotated class
        delivery: Literal["at-least-once", "exactly-once"] = "at-least-once",
    ) -> None:
        self.channel = channel
        self.storage = storage
        self.delivery = delivery

        self.__skaal_pattern__ = {
            "pattern_type": "outbox",
            "channel": channel,
            "storage": storage,
            "delivery": delivery,
        }

    def __repr__(self) -> str:
        storage_name = getattr(self.storage, "__name__", repr(self.storage))
        return f"Outbox(storage={storage_name!r}, delivery={self.delivery!r})"
