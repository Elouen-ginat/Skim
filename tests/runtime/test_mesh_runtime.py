"""Tests for MeshRuntime and the skaal_mesh native extension.

All tests skip when the ``skaal_mesh`` extension is not installed so the CI
suite remains green on machines without a Rust toolchain.
"""

from __future__ import annotations

import json

import pytest

try:
    import skaal_mesh  # type: ignore[import-untyped]

    HAS_MESH = True
except ImportError:
    HAS_MESH = False

pytestmark = pytest.mark.skipif(not HAS_MESH, reason="skaal_mesh extension not installed")


# ── Low-level Rust extension tests ──────────────────────────────────────────


class TestSkaalMeshExtension:
    def test_create_and_health(self) -> None:
        m = skaal_mesh.SkaalMesh("test-app", "")
        h = json.loads(m.health_snapshot())
        assert h["app"] == "test-app"
        assert h["status"] == "ok"
        assert set(h) >= {"agents", "state", "migrations", "channels"}

    def test_register_and_list_agents(self) -> None:
        m = skaal_mesh.SkaalMesh("test-app", "")
        m.register_agent("Counter", "c-1", 0, None)
        m.register_agent("Worker", "w-1", 0, None)
        agents = json.loads(m.list_agents(None, None))
        assert {a["agent_id"] for a in agents} == {"c-1", "w-1"}

        workers = json.loads(m.list_agents("Worker", None))
        assert [a["agent_id"] for a in workers] == ["w-1"]

    def test_route_agent_call_marks_running(self) -> None:
        m = skaal_mesh.SkaalMesh("test-app", "")
        m.register_agent("User", "u-1", 0, None)
        routed = json.loads(m.route_agent_call("User", "u-1", "greet", "{}"))
        assert routed["status"] == "routed"
        assert routed["agent_id"] == "u-1"

    def test_channel_publish_no_subscribers(self) -> None:
        m = skaal_mesh.SkaalMesh("test-app", "")
        # No subscribers yet — publish returns 0 receivers without error.
        assert m.publish("events", '{"type":"click"}') == 0


# ── MeshRuntime integration tests ───────────────────────────────────────────


class TestMeshRuntime:
    @pytest.mark.asyncio
    async def test_dispatch_and_health(self) -> None:
        from skaal import App
        from skaal.runtime.mesh_runtime import MeshRuntime

        app = App("mesh-test")

        @app.function
        async def greet(name: str = "world") -> dict:
            return {"hello": name}

        rt = MeshRuntime(app, plan_json=json.dumps({"compute": {"greet": {}}}))

        # Health endpoint includes mesh info.
        result, status = await rt._dispatch("GET", "/health", b"")
        assert status == 200
        assert result["status"] == "ok"
        assert "mesh" in result

        # Function invocation through the dispatch path.
        result, status = await rt._dispatch("POST", "/greet", json.dumps({"name": "mesh"}).encode())
        assert status == 200
        assert result == {"hello": "mesh"}

        await rt.shutdown()

    @pytest.mark.asyncio
    async def test_mesh_bridge_methods(self) -> None:
        from skaal import App
        from skaal.runtime.mesh_runtime import MeshRuntime

        app = App("bridge-test")

        @app.function
        async def noop() -> dict:
            return {}

        rt = MeshRuntime(app)

        # channel_publish returns receiver count (0 with no subscribers).
        assert rt.channel_publish("t", {"x": 1}) == 0

        # health returns the new mesh JSON shape.
        h = rt.health()
        assert h["app"] == "bridge-test"
        assert "agents" in h

        await rt.shutdown()
