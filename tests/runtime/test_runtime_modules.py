"""Tests for skaal.runtime modules: state, channels, agent_registry."""

from __future__ import annotations

import asyncio

import pytest

from skaal.runtime.agent_registry import AgentRegistry, AgentStatus
from skaal.runtime.channels import LocalChannel
from skaal.runtime.state import InMemoryStateStore


# ── InMemoryStateStore ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_state_store_get_missing():
    store = InMemoryStateStore()
    assert await store.get("missing") is None


@pytest.mark.asyncio
async def test_state_store_set_get():
    store = InMemoryStateStore()
    await store.set("key", {"score": 42})
    assert await store.get("key") == {"score": 42}


@pytest.mark.asyncio
async def test_state_store_delete():
    store = InMemoryStateStore()
    await store.set("x", 1)
    await store.delete("x")
    assert await store.get("x") is None


@pytest.mark.asyncio
async def test_state_store_exists():
    store = InMemoryStateStore()
    assert not await store.exists("k")
    await store.set("k", True)
    assert await store.exists("k")


@pytest.mark.asyncio
async def test_state_store_keys_prefix():
    store = InMemoryStateStore()
    await store.set("user:1", "alice")
    await store.set("user:2", "bob")
    await store.set("order:1", "o1")
    user_keys = await store.keys("user:")
    assert set(user_keys) == {"user:1", "user:2"}


@pytest.mark.asyncio
async def test_state_store_overwrite():
    store = InMemoryStateStore()
    await store.set("k", "old")
    await store.set("k", "new")
    assert await store.get("k") == "new"


# ── LocalChannel ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_channel_publish_subscribe():
    ch = LocalChannel()
    received = []

    async def consumer():
        async for msg in ch.subscribe("topic"):
            received.append(msg)
            break  # receive one message then stop

    consumer_task = asyncio.create_task(consumer())
    await asyncio.sleep(0)  # yield so consumer is waiting
    await ch.publish("topic", {"event": "hello"})
    await asyncio.wait_for(consumer_task, timeout=1.0)
    assert received == [{"event": "hello"}]


@pytest.mark.asyncio
async def test_channel_multiple_consumers():
    ch = LocalChannel()
    results_a: list = []
    results_b: list = []

    async def consumer_a():
        async for msg in ch.subscribe("t"):
            results_a.append(msg)
            break

    async def consumer_b():
        async for msg in ch.subscribe("t"):
            results_b.append(msg)
            break

    task_a = asyncio.create_task(consumer_a())
    task_b = asyncio.create_task(consumer_b())
    await asyncio.sleep(0)
    await ch.publish("t", "broadcast")
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=1.0)
    assert results_a == ["broadcast"]
    assert results_b == ["broadcast"]


@pytest.mark.asyncio
async def test_channel_no_subscribers():
    """Publish with no subscribers should not raise."""
    ch = LocalChannel()
    await ch.publish("empty-topic", "msg")  # no-op


@pytest.mark.asyncio
async def test_channel_subscriber_cleanup():
    """After consumer exits, its queue is removed from the channel."""
    ch = LocalChannel()

    async def one_shot():
        async for _ in ch.subscribe("t"):
            break

    task = asyncio.create_task(one_shot())
    await asyncio.sleep(0)
    await ch.publish("t", "go")
    await asyncio.wait_for(task, timeout=1.0)
    # After consumer done, topic queue list should be empty
    assert ch._queues.get("t", []) == []


# ── AgentRegistry ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_registry_register():
    reg = AgentRegistry()
    record = await reg.register("a1", "my_fn", instance=0)
    assert record.agent_id == "a1"
    assert record.function_name == "my_fn"
    assert record.status == AgentStatus.STARTING


@pytest.mark.asyncio
async def test_agent_registry_update_status():
    reg = AgentRegistry()
    await reg.register("a1", "fn")
    await reg.update_status("a1", AgentStatus.RUNNING)
    record = await reg.get("a1")
    assert record is not None
    assert record.status == AgentStatus.RUNNING


@pytest.mark.asyncio
async def test_agent_registry_deregister():
    reg = AgentRegistry()
    await reg.register("a1", "fn")
    await reg.deregister("a1")
    assert await reg.get("a1") is None


@pytest.mark.asyncio
async def test_agent_registry_list_by_function():
    reg = AgentRegistry()
    await reg.register("a1", "fn_a")
    await reg.register("a2", "fn_b")
    await reg.register("a3", "fn_a")
    results = await reg.list_agents(function_name="fn_a")
    ids = {r.agent_id for r in results}
    assert ids == {"a1", "a3"}


@pytest.mark.asyncio
async def test_agent_registry_list_by_status():
    reg = AgentRegistry()
    await reg.register("a1", "fn")
    await reg.register("a2", "fn")
    await reg.update_status("a2", AgentStatus.RUNNING)
    running = await reg.list_agents(status=AgentStatus.RUNNING)
    assert len(running) == 1
    assert running[0].agent_id == "a2"


@pytest.mark.asyncio
async def test_agent_registry_get_missing():
    reg = AgentRegistry()
    assert await reg.get("nonexistent") is None


@pytest.mark.asyncio
async def test_agent_registry_metadata():
    reg = AgentRegistry()
    await reg.register("a1", "fn", metadata={"region": "eu-west-1"})
    record = await reg.get("a1")
    assert record is not None
    assert record.metadata["region"] == "eu-west-1"
