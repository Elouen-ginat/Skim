"""Tests for pattern engines (EventLog / Projection / Saga / Outbox)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from skaal.backends.local_backend import LocalMap
from skaal.patterns import EventLog, Outbox, Saga, SagaStep
from skaal.runtime._observer import InMemoryRuntimeObserver
from skaal.runtime.engines.eventlog import EventLogEngine
from skaal.runtime.engines.outbox import OutboxEngine
from skaal.runtime.engines.projection import ProjectionEngine
from skaal.runtime.engines.saga import SagaEngine, SagaExecutor

# ── EventLog push-based subscribe ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_eventlog_subscribe_wakes_on_append() -> None:
    log: EventLog[dict] = EventLog(LocalMap())

    received: list[dict] = []

    async def consume() -> None:
        async for _off, ev in log.subscribe("g1", poll_interval=5.0):
            received.append(ev)
            if len(received) == 2:
                return

    task = asyncio.create_task(consume())
    # Give the subscriber a moment to park on the notify Event.
    await asyncio.sleep(0.01)
    await log.append({"a": 1})
    await log.append({"a": 2})
    # Both appends should wake the subscriber quickly; far below poll_interval.
    await asyncio.wait_for(task, timeout=1.0)
    assert received == [{"a": 1}, {"a": 2}]


@pytest.mark.asyncio
async def test_eventlog_engine_start_stop() -> None:
    log: EventLog[dict] = EventLog(LocalMap())
    engine = EventLogEngine(log)
    observer = InMemoryRuntimeObserver()
    await engine.start(SimpleNamespace(observer=observer))
    await engine.stop()

    snapshot = observer.snapshot()
    engines = snapshot["engines"]
    engine_name = next(iter(engines))
    assert engine_name.startswith("eventlog:")
    assert engines[engine_name]["starts"] == 1
    assert engines[engine_name]["stops"] == 1


# ── Projection ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_projection_engine_applies_handler_to_each_event() -> None:
    source: EventLog[dict] = EventLog(LocalMap())

    class Summary:
        totals: dict[str, int] = {}

    async def apply(target: type, event: dict) -> None:
        k = event["k"]
        target.totals[k] = target.totals.get(k, 0) + event["delta"]

    # Fake Projection; we only need .source / .target / .handler / .checkpoint_every
    proj = SimpleNamespace(
        source=source,
        target=Summary,
        handler="apply",
        checkpoint_every=1,
        strict=False,
    )
    engine = ProjectionEngine(proj)  # type: ignore[arg-type]
    context = SimpleNamespace(functions={"apply": apply}, observer=InMemoryRuntimeObserver())
    await engine.start(context)

    await asyncio.sleep(0.01)
    await source.append({"k": "x", "delta": 1})
    await source.append({"k": "x", "delta": 2})
    await source.append({"k": "y", "delta": 5})

    # Poll until the projection catches up.
    for _ in range(50):
        if Summary.totals.get("x") == 3 and Summary.totals.get("y") == 5:
            break
        await asyncio.sleep(0.02)
    assert Summary.totals == {"x": 3, "y": 5}
    await engine.stop()


@pytest.mark.asyncio
async def test_projection_engine_persists_checkpoint_to_target_backend() -> None:
    source: EventLog[dict] = EventLog(LocalMap())

    class SummaryStore:
        _backend = LocalMap()

    async def apply(_target: type, _event: dict) -> None:
        return None

    proj = SimpleNamespace(
        source=source,
        target=SummaryStore,
        handler="apply",
        checkpoint_every=2,
        strict=False,
    )
    engine = ProjectionEngine(proj)  # type: ignore[arg-type]
    await engine.start(
        SimpleNamespace(functions={"apply": apply}, observer=InMemoryRuntimeObserver())
    )

    await asyncio.sleep(0.01)
    await source.append({"id": 1})
    await source.append({"id": 2})

    for _ in range(50):
        offset = await SummaryStore._backend.get("__projection__:apply:offset")
        if offset == 1:
            break
        await asyncio.sleep(0.02)

    assert await SummaryStore._backend.get("__projection__:apply:offset") == 1
    await engine.stop()


@pytest.mark.asyncio
async def test_projection_engine_strict_mode_surfaces_failures() -> None:
    source: EventLog[dict] = EventLog(LocalMap())
    observer = InMemoryRuntimeObserver()

    class SummaryStore:
        _backend = LocalMap()

    async def apply(_target: type, _event: dict) -> None:
        raise RuntimeError("projection failed")

    proj = SimpleNamespace(
        source=source,
        target=SummaryStore,
        handler="apply",
        checkpoint_every=1,
        strict=True,
    )
    engine = ProjectionEngine(proj)  # type: ignore[arg-type]
    await engine.start(SimpleNamespace(functions={"apply": apply}, observer=observer))

    await asyncio.sleep(0.01)
    await source.append({"id": 1})

    with pytest.raises(RuntimeError, match="projection failed"):
        assert engine._task is not None
        await asyncio.wait_for(engine._task, timeout=1.0)

    stream = observer.snapshot()["events"]["streams"]["apply"]
    assert stream["failed"] == 1
    assert stream["last_error"] == "RuntimeError('projection failed')"


# ── Saga ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_saga_happy_path_runs_every_step() -> None:
    calls: list[str] = []

    async def reserve(**_kw: object) -> dict:
        calls.append("reserve")
        return {"reserved": True}

    async def charge(**_kw: object) -> dict:
        calls.append("charge")
        return {"charged": True}

    async def release(**_kw: object) -> None:
        calls.append("release")

    async def refund(**_kw: object) -> None:
        calls.append("refund")

    saga = Saga(
        "buy",
        steps=[
            SagaStep("reserve", compensate="release"),
            SagaStep("charge", compensate="refund"),
        ],
    )
    executor = SagaExecutor(
        saga, functions={"reserve": reserve, "charge": charge, "release": release, "refund": refund}
    )
    state = await executor.run(order_id=1)
    assert state["status"] == "completed"
    assert calls == ["reserve", "charge"]


@pytest.mark.asyncio
async def test_saga_rolls_back_on_failure() -> None:
    calls: list[str] = []

    async def reserve(**_kw: object) -> dict:
        calls.append("reserve")
        return {"ok": True}

    async def charge(**_kw: object) -> dict:
        calls.append("charge")
        raise RuntimeError("card declined")

    async def release(**_kw: object) -> None:
        calls.append("release")

    async def refund(**_kw: object) -> None:
        calls.append("refund")

    saga = Saga(
        "buy",
        steps=[
            SagaStep("reserve", compensate="release"),
            SagaStep("charge", compensate="refund"),
        ],
    )
    executor = SagaExecutor(
        saga, functions={"reserve": reserve, "charge": charge, "release": release, "refund": refund}
    )
    with pytest.raises(Exception):
        await executor.run(order_id=1)
    # Only the first step ran; its compensator must have been called.
    assert "reserve" in calls
    assert "release" in calls
    assert "refund" not in calls  # never committed, no compensation


@pytest.mark.asyncio
async def test_saga_engine_registers_executor_on_context() -> None:
    saga = Saga("noop", steps=[])
    engine = SagaEngine(saga)
    ctx = SimpleNamespace(functions={}, stores={})
    await engine.start(ctx)
    assert "noop" in ctx.sagas
    assert isinstance(ctx.sagas["noop"], SagaExecutor)


# ── Outbox ───────────────────────────────────────────────────────────────────


class _FakeChannel:
    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send(self, payload: object) -> None:
        self.sent.append(payload)


class _Storage:
    _backend = None  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_outbox_relay_publishes_pending_rows() -> None:
    backend = LocalMap()
    _Storage._backend = backend  # wire directly to skip full @storage plumbing
    chan = _FakeChannel()
    ob = Outbox(channel=chan, storage=_Storage, delivery="at-least-once")

    engine = OutboxEngine(ob, poll_interval=0.01)
    await engine.start(SimpleNamespace())

    # After start(), ob.write is installed.
    await ob.write("order-1", {"event": "placed", "id": 1})  # type: ignore[attr-defined]
    await ob.write("order-2", {"event": "placed", "id": 2})  # type: ignore[attr-defined]

    # Wait for the relay worker to drain.
    for _ in range(50):
        if len(chan.sent) == 2:
            break
        await asyncio.sleep(0.02)
    assert sorted(chan.sent, key=lambda p: p["id"]) == [  # type: ignore[index]
        {"event": "placed", "id": 1},
        {"event": "placed", "id": 2},
    ]

    # At-least-once → rows deleted after delivery.
    remaining = await backend.scan("outbox:")
    assert remaining == []

    await engine.stop()


@pytest.mark.asyncio
async def test_outbox_restart_rebinds_write_helper_to_current_backend() -> None:
    backend_one = LocalMap()
    backend_two = LocalMap()
    chan = _FakeChannel()
    ob = Outbox(channel=chan, storage=_Storage, delivery="at-least-once")

    _Storage._backend = backend_one
    engine = OutboxEngine(ob, poll_interval=0.01)
    await engine.start(SimpleNamespace())
    await ob.write("order-1", {"event": "placed", "id": 1})  # type: ignore[attr-defined]

    for _ in range(50):
        if len(chan.sent) == 1:
            break
        await asyncio.sleep(0.02)

    await engine.stop()

    _Storage._backend = backend_two
    await engine.start(SimpleNamespace())
    await ob.write("order-2", {"event": "placed", "id": 2})  # type: ignore[attr-defined]

    for _ in range(50):
        if len(chan.sent) == 2:
            break
        await asyncio.sleep(0.02)

    assert [payload["id"] for payload in chan.sent] == [1, 2]  # type: ignore[index]
    assert await backend_one.scan("outbox:") == []
    assert await backend_two.scan("outbox:") == []

    await engine.stop()
