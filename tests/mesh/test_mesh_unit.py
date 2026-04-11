"""Unit tests for the Skaal runtime mesh Python API.

These tests exercise the MeshClient (Python layer) against the compiled
skaal_mesh extension.  They require the extension to be built first::

    maturin develop --manifest-path mesh/Cargo.toml

If the extension is not available the entire module is skipped.
"""

from __future__ import annotations

import json

import pytest

# ── Skip guard ────────────────────────────────────────────────────────────────

try:
    import skaal_mesh as _ext  # noqa: F401

    _EXT_AVAILABLE = True
except ImportError:
    _EXT_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _EXT_AVAILABLE,
    reason="skaal_mesh extension not built — run: maturin develop --manifest-path mesh/Cargo.toml",
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def raw_mesh():
    """A bare SkaalMesh Rust object."""
    return _ext.SkaalMesh("testapp", "")


@pytest.fixture
def mesh():
    """A MeshClient wrapping a fresh SkaalMesh."""
    from skaal.mesh import MeshClient

    return MeshClient("testapp")


# ── Raw extension tests ───────────────────────────────────────────────────────


class TestRawMesh:
    def test_constructor_empty_plan(self, raw_mesh):
        assert raw_mesh.app_name == "testapp"

    def test_constructor_with_plan_json(self):
        plan = {"app_name": "planapp", "backends": {}, "functions": [], "agents": []}
        m = _ext.SkaalMesh("planapp", json.dumps(plan))
        assert m.app_name == "planapp"

    def test_repr(self, raw_mesh):
        r = repr(raw_mesh)
        assert "SkaalMesh" in r
        assert "testapp" in r

    def test_health_snapshot_structure(self, raw_mesh):
        snap = json.loads(raw_mesh.health_snapshot())
        assert snap["app"] == "testapp"
        assert snap["status"] == "ok"
        assert "agents" in snap
        assert "state" in snap
        assert "migrations" in snap
        assert "channels" in snap

    # ── Agent Registry ────────────────────────────────────────────────────────

    def test_register_agent(self, raw_mesh):
        raw = raw_mesh.register_agent("Counter", "c-1", 0, None)
        data = json.loads(raw)
        assert data["agent_id"] == "c-1"
        assert data["agent_type"] == "Counter"
        assert data["status"] == "starting"

    def test_register_duplicate_raises(self, raw_mesh):
        raw_mesh.register_agent("Counter", "c-1", 0, None)
        with pytest.raises(RuntimeError, match="already registered"):
            raw_mesh.register_agent("Counter", "c-1", 0, None)

    def test_update_status(self, raw_mesh):
        raw_mesh.register_agent("Counter", "c-1", 0, None)
        raw_mesh.update_agent_status("c-1", "running")
        data = json.loads(raw_mesh.get_agent("c-1"))
        assert data["status"] == "running"

    def test_invalid_status_raises(self, raw_mesh):
        raw_mesh.register_agent("Counter", "c-1", 0, None)
        with pytest.raises(ValueError, match="Unknown status"):
            raw_mesh.update_agent_status("c-1", "flying")

    def test_deregister(self, raw_mesh):
        raw_mesh.register_agent("Counter", "c-1", 0, None)
        raw_mesh.deregister_agent("c-1")
        assert raw_mesh.get_agent("c-1") is None

    def test_list_agents_filter_type(self, raw_mesh):
        raw_mesh.register_agent("Counter", "c-1", 0, None)
        raw_mesh.register_agent("Worker", "w-1", 0, None)

        counters = json.loads(raw_mesh.list_agents("Counter", None))
        assert len(counters) == 1
        assert counters[0]["agent_type"] == "Counter"

    def test_route_call_to_unknown_agent(self, raw_mesh):
        with pytest.raises(KeyError):
            raw_mesh.route_agent_call("Counter", "ghost", "inc", "{}")

    def test_route_call_success(self, raw_mesh):
        raw_mesh.register_agent("Counter", "c-1", 0, None)
        result = json.loads(raw_mesh.route_agent_call("Counter", "c-1", "inc", "{}"))
        assert result["status"] == "routed"
        assert result["method"] == "inc"

    # ── State Store ───────────────────────────────────────────────────────────

    def test_state_set_get(self, raw_mesh):
        raw_mesh.state_set("foo", "42")
        assert json.loads(raw_mesh.state_get("foo")) == 42

    def test_state_missing_returns_none(self, raw_mesh):
        assert raw_mesh.state_get("does-not-exist") is None

    def test_state_delete(self, raw_mesh):
        raw_mesh.state_set("x", '"hello"')
        raw_mesh.state_delete("x")
        assert raw_mesh.state_get("x") is None

    def test_state_exists(self, raw_mesh):
        assert not raw_mesh.state_exists("k")
        raw_mesh.state_set("k", "true")
        assert raw_mesh.state_exists("k")

    def test_state_keys_prefix(self, raw_mesh):
        raw_mesh.state_set("agent:1", '"a"')
        raw_mesh.state_set("agent:2", '"b"')
        raw_mesh.state_set("other:1", '"c"')
        keys = raw_mesh.state_keys("agent:")
        assert sorted(keys) == ["agent:1", "agent:2"]

    def test_state_invalid_json_raises(self, raw_mesh):
        with pytest.raises(ValueError):
            raw_mesh.state_set("k", "not-json{{{")

    # ── Migration ─────────────────────────────────────────────────────────────

    def test_migration_start(self, raw_mesh):
        state = json.loads(raw_mesh.start_migration("v.Counts", "sqlite", "redis"))
        assert state["stage"] == 1
        assert state["stage_name"] == "shadow_write"
        assert state["source_backend"] == "sqlite"
        assert state["target_backend"] == "redis"

    def test_migration_full_cycle(self, raw_mesh):
        raw_mesh.start_migration("v", "a", "b")
        for expected_stage in range(2, 7):
            state = json.loads(raw_mesh.advance_migration("v", 0, 0))
            assert state["stage"] == expected_stage
        assert state["stage_name"] == "done"

    def test_migration_rollback(self, raw_mesh):
        raw_mesh.start_migration("v", "a", "b")
        raw_mesh.advance_migration("v", 0, 0)  # stage 2
        state = json.loads(raw_mesh.rollback_migration("v"))
        assert state["stage"] == 1

    def test_duplicate_migration_raises(self, raw_mesh):
        raw_mesh.start_migration("v", "a", "b")
        with pytest.raises(ValueError, match="already in progress"):
            raw_mesh.start_migration("v", "a", "b")

    def test_advance_past_done_raises(self, raw_mesh):
        raw_mesh.start_migration("v", "a", "b")
        for _ in range(5):
            raw_mesh.advance_migration("v", 0, 0)
        with pytest.raises(ValueError, match="already complete"):
            raw_mesh.advance_migration("v", 0, 0)

    def test_get_migration_none_when_absent(self, raw_mesh):
        assert raw_mesh.get_migration("nonexistent") is None

    def test_keys_migrated_accumulates(self, raw_mesh):
        raw_mesh.start_migration("v", "a", "b")
        raw_mesh.advance_migration("v", 0, 50)
        state = json.loads(raw_mesh.advance_migration("v", 0, 30))
        assert state["keys_migrated"] == 80

    # ── Channels ─────────────────────────────────────────────────────────────

    def test_publish_no_subscribers(self, raw_mesh):
        receivers = raw_mesh.publish("events", '{"type": "ping"}')
        assert receivers == 0

    def test_health_reflects_channels(self, raw_mesh):
        raw_mesh.publish("a", '"x"')
        snap = json.loads(raw_mesh.health_snapshot())
        assert snap["channels"]["topics"] >= 1


# ── MeshClient (Python wrapper) tests ─────────────────────────────────────────


class TestMeshClient:
    def test_register_and_get(self, mesh):
        from skaal.mesh import AgentInfo

        info = mesh.register_agent("Counter", "c-1")
        assert isinstance(info, AgentInfo)
        assert info.agent_id == "c-1"
        assert info.status == "starting"

    def test_update_status(self, mesh):
        mesh.register_agent("Counter", "c-1")
        mesh.update_agent_status("c-1", "running")
        info = mesh.get_agent("c-1")
        assert info is not None
        assert info.status == "running"

    def test_list_agents_typed(self, mesh):
        from skaal.mesh import AgentInfo

        mesh.register_agent("Counter", "c-1")
        mesh.register_agent("Counter", "c-2")
        agents = mesh.list_agents(agent_type="Counter")
        assert all(isinstance(a, AgentInfo) for a in agents)
        assert len(agents) == 2

    def test_route_returns_routing_info(self, mesh):
        from skaal.mesh import RoutingInfo

        mesh.register_agent("Counter", "c-1")
        mesh.update_agent_status("c-1", "running")
        r = mesh.route_agent_call("Counter", "c-1", "increment", {"delta": 1})
        assert isinstance(r, RoutingInfo)
        assert r.status == "routed"

    def test_state_store_typed(self, mesh):
        mesh.state_set("hits", 0)
        mesh.state_set("hits", mesh.state_get("hits") + 1)
        assert mesh.state_get("hits") == 1

    def test_state_complex_value(self, mesh):
        mesh.state_set("config", {"max": 100, "tags": ["a", "b"]})
        got = mesh.state_get("config")
        assert got["max"] == 100
        assert got["tags"] == ["a", "b"]

    def test_migration_typed(self, mesh):
        from skaal.mesh import MigrationState

        state = mesh.start_migration("v.Counts", "sqlite", "redis")
        assert isinstance(state, MigrationState)
        assert state.stage == 1
        assert state.stage_name == "shadow_write"

        state = mesh.advance_migration("v.Counts", keys_migrated=100)
        assert state.stage == 2
        assert state.keys_migrated == 100

    def test_migration_is_complete_property(self, mesh):
        mesh.start_migration("v", "a", "b")
        for _ in range(5):
            state = mesh.advance_migration("v")
        assert state.is_complete

    def test_health_snapshot_typed(self, mesh):
        from skaal.mesh import HealthSnapshot

        snap = mesh.health_snapshot()
        assert isinstance(snap, HealthSnapshot)
        assert snap.app == "testapp"
        assert snap.status == "ok"
        assert "total" in snap.agents

    def test_health_snapshot_reflects_agents(self, mesh):
        mesh.register_agent("Worker", "w-1")
        mesh.update_agent_status("w-1", "running")
        snap = mesh.health_snapshot()
        assert snap.agents["total"] >= 1
        assert snap.agents["running"] >= 1

    def test_publish(self, mesh):
        receivers = mesh.publish("events", {"type": "test"})
        assert receivers == 0  # no subscribers in this test

    def test_repr(self, mesh):
        assert "testapp" in repr(mesh)

    def test_metadata_stored(self, mesh):
        mesh.register_agent("Worker", "w-1", metadata={"queue": "fast"})
        info = mesh.get_agent("w-1")
        assert info is not None
        assert info.metadata["queue"] == "fast"
