from __future__ import annotations

import asyncio
import json
from typing import Annotated

import pytest

from skaal import Agent, App, handler
from skaal.runtime.local import LocalRuntime
from skaal.types import Persistent


def _make_counter_agent_app(name: str = "agent-counter") -> App:
    app = App(name)

    @app.agent(persistent=True)
    class CounterAgent(Agent):
        count: Persistent[int] = 0
        transient: int = 0

        @handler
        async def add(self, amount: int = 1) -> int:
            current = self.count
            await asyncio.sleep(0)
            self.count = current + amount
            self.transient += amount
            return self.count

        @handler
        async def snapshot(self) -> dict[str, int]:
            return {"count": self.count, "transient": self.transient}

    return app


def _make_ephemeral_agent_app(name: str = "agent-ephemeral") -> App:
    app = App(name)

    @app.agent(persistent=False)
    class EphemeralAgent(Agent):
        count: Persistent[int] = 0

        @handler
        async def add(self, amount: int = 1) -> int:
            self.count += amount
            return self.count

        @handler
        async def snapshot(self) -> int:
            return self.count

    return app


@pytest.mark.asyncio
async def test_persistent_agent_state_survives_runtime_restart(tmp_path) -> None:
    db_path = tmp_path / "agents.db"

    runtime = LocalRuntime.from_sqlite(_make_counter_agent_app(), db_path)
    assert await runtime.invoke_agent("CounterAgent", "user-1", "add", 2) == 2
    await runtime.shutdown()

    restarted = LocalRuntime.from_sqlite(_make_counter_agent_app(), db_path)
    state = await restarted.invoke_agent("CounterAgent", "user-1", "snapshot")
    assert state == {"count": 2, "transient": 0}
    await restarted.shutdown()


@pytest.mark.asyncio
async def test_persistent_agent_http_route_round_trips_args_and_kwargs(tmp_path) -> None:
    runtime = LocalRuntime.from_sqlite(_make_counter_agent_app("agent-http"), tmp_path / "http.db")
    body = json.dumps({"args": [3], "kwargs": {}}).encode()

    data, status = await runtime._dispatch(
        "POST",
        "/_skaal/agents/CounterAgent/user-1/add",
        body,
    )

    assert status == 200
    assert data == 3
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_persistent_agent_serializes_same_identity_calls() -> None:
    runtime = LocalRuntime(_make_counter_agent_app("agent-locks"))

    results = await asyncio.gather(
        runtime.invoke_agent("CounterAgent", "same", "add", 1),
        runtime.invoke_agent("CounterAgent", "same", "add", 1),
    )

    assert sorted(results) == [1, 2]
    assert await runtime.invoke_agent("CounterAgent", "same", "snapshot") == {
        "count": 2,
        "transient": 0,
    }
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_persistent_agent_keeps_different_identities_isolated() -> None:
    runtime = LocalRuntime(_make_counter_agent_app("agent-isolated"))

    results = await asyncio.gather(
        runtime.invoke_agent("CounterAgent", "left", "add", 1),
        runtime.invoke_agent("CounterAgent", "right", "add", 1),
    )

    assert results == [1, 1]
    assert await runtime.invoke_agent("CounterAgent", "left", "snapshot") == {
        "count": 1,
        "transient": 0,
    }
    assert await runtime.invoke_agent("CounterAgent", "right", "snapshot") == {
        "count": 1,
        "transient": 0,
    }
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_non_persistent_agent_does_not_write_state(tmp_path) -> None:
    db_path = tmp_path / "ephemeral.db"

    runtime = LocalRuntime.from_sqlite(_make_ephemeral_agent_app(), db_path)
    assert await runtime.invoke_agent("EphemeralAgent", "user-1", "add", 4) == 4
    await runtime.shutdown()

    restarted = LocalRuntime.from_sqlite(_make_ephemeral_agent_app(), db_path)
    assert await restarted.invoke_agent("EphemeralAgent", "user-1", "snapshot") == 0
    await restarted.shutdown()


@pytest.mark.asyncio
async def test_persistent_agent_creates_unknown_identity_on_first_call() -> None:
    runtime = LocalRuntime(_make_counter_agent_app("agent-new-identity"))

    assert await runtime.invoke_agent("CounterAgent", "new-user", "snapshot") == {
        "count": 0,
        "transient": 0,
    }
    await runtime.shutdown()


def test_agent_supports_annotated_persistent_fields() -> None:
    class AnnotatedAgent(Agent):
        count: Annotated[int, Persistent] = 0
        transient: int = 0

    assert AnnotatedAgent.__skaal_persistent_fields__ == {"count"}
