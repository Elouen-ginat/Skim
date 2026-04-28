"""Tests for EventLog pattern with LocalMap backend."""

from __future__ import annotations

import pytest

from skaal.backends.kv.local_map import LocalMap
from skaal.patterns import EventLog

# ── append ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_append_returns_sequential_offsets():
    log: EventLog[str] = EventLog(_backend=LocalMap())

    offset0 = await log.append("event_a")
    offset1 = await log.append("event_b")
    offset2 = await log.append("event_c")

    assert offset0 == 0
    assert offset1 == 1
    assert offset2 == 2


@pytest.mark.asyncio
async def test_append_single_event():
    log: EventLog[dict] = EventLog(_backend=LocalMap())
    offset = await log.append({"type": "order_created", "id": 1})
    assert offset == 0


# ── replay ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_replay_yields_events_in_order():
    log: EventLog[str] = EventLog(_backend=LocalMap())

    await log.append("first")
    await log.append("second")
    await log.append("third")

    results = []
    async for offset, event in log.replay(from_offset=0):
        results.append((offset, event))

    assert results == [(0, "first"), (1, "second"), (2, "third")]


@pytest.mark.asyncio
async def test_replay_from_offset_skips_earlier():
    log: EventLog[str] = EventLog(_backend=LocalMap())

    await log.append("zero")
    await log.append("one")
    await log.append("two")
    await log.append("three")

    results = []
    async for offset, event in log.replay(from_offset=2):
        results.append((offset, event))

    assert results == [(2, "two"), (3, "three")]


@pytest.mark.asyncio
async def test_replay_empty_log():
    log: EventLog[str] = EventLog(_backend=LocalMap())
    results = []
    async for offset, event in log.replay(from_offset=0):
        results.append((offset, event))
    assert results == []


@pytest.mark.asyncio
async def test_replay_from_offset_beyond_end():
    log: EventLog[str] = EventLog(_backend=LocalMap())
    await log.append("only_event")

    results = []
    async for offset, event in log.replay(from_offset=99):
        results.append((offset, event))
    assert results == []


# ── subscribe ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_subscribe_yields_new_events():
    """Subscribe should yield events that are already in the log and then stop when
    we cancel the generator."""
    log: EventLog[str] = EventLog(_backend=LocalMap())

    # Pre-populate two events
    await log.append("alpha")
    await log.append("beta")

    results = []
    gen = log.subscribe("grp1", from_beginning=True, poll_interval=0.01)
    # Collect up to 2 events then cancel
    async for offset, event in gen:
        results.append((offset, event))
        if len(results) >= 2:
            break

    assert results == [(0, "alpha"), (1, "beta")]


@pytest.mark.asyncio
async def test_subscribe_consumer_group_tracks_offset():
    """A consumer group should resume from where it left off."""
    backend = LocalMap()
    log: EventLog[str] = EventLog(_backend=backend)

    await log.append("msg0")
    await log.append("msg1")
    await log.append("msg2")

    # First subscription: read first two messages
    results1 = []
    async for offset, event in log.subscribe("grp_a", from_beginning=True, poll_interval=0.01):
        results1.append((offset, event))
        if len(results1) >= 2:
            break

    assert results1[0] == (0, "msg0")
    assert results1[1] == (1, "msg1")

    # Second subscription with same group: should resume from offset 2
    results2 = []
    async for offset, event in log.subscribe("grp_a", poll_interval=0.01):
        results2.append((offset, event))
        if len(results2) >= 1:
            break

    assert results2 == [(2, "msg2")]


@pytest.mark.asyncio
async def test_subscribe_from_beginning_false_starts_at_current():
    """Without from_beginning, subscription starts from stored consumer offset."""
    backend = LocalMap()
    log: EventLog[str] = EventLog(_backend=backend)

    await log.append("old_event")

    # No prior offset for this group — default offset is 0
    # Read one event to advance the offset
    async for offset, event in log.subscribe("grp_b", from_beginning=True, poll_interval=0.01):
        break

    # Now append a new event and subscribe without from_beginning
    await log.append("new_event")

    results = []
    async for offset, event in log.subscribe("grp_b", poll_interval=0.01):
        results.append((offset, event))
        break

    assert len(results) == 1
    assert results[0][1] == "new_event"


# ── EventLog default backend ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_event_log_default_backend_works_without_setup():
    """EventLog with default LocalMap backend works without any runtime setup."""
    log: EventLog[int] = EventLog()  # uses default LocalMap

    for i in range(5):
        await log.append(i)

    results = []
    async for offset, event in log.replay(from_offset=0):
        results.append(event)

    assert results == [0, 1, 2, 3, 4]


# ── repr ───────────────────────────────────────────────────────────────────────


def test_event_log_repr():
    log = EventLog(retention="30d", partitions=8)
    r = repr(log)
    assert "EventLog" in r
    assert "30d" in r
    assert "8" in r
