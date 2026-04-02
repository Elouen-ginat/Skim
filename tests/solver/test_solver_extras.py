"""Tests for solver/graph.py, solver/explain.py, and solver/stability.py."""

from __future__ import annotations

import pytest

from skaal import App
from skaal.plan import ComputeSpec, PlanFile, StorageSpec
from skaal.solver.explain import explain_plan
from skaal.solver.graph import CyclicDependencyError, ResourceGraph, build_graph
from skaal.solver.stability import (
    PlanDiff,
    StabilityVerdict,
    diff_plans,
)
from skaal.types import Compute


# ── ResourceGraph ─────────────────────────────────────────────────────────────

def test_resource_graph_empty_topological():
    g = ResourceGraph()
    assert g.topological_order() == []


def test_resource_graph_single_node():
    g = ResourceGraph()
    g.add_node("A")
    assert g.topological_order() == ["A"]


def test_resource_graph_linear_chain():
    g = ResourceGraph()
    # A depends on B depends on C → order: C, B, A
    g.add_edge("A", "B")
    g.add_edge("B", "C")
    order = g.topological_order()
    assert order.index("C") < order.index("B") < order.index("A")


def test_resource_graph_cycle_detection():
    g = ResourceGraph()
    g.add_edge("A", "B")
    g.add_edge("B", "A")
    with pytest.raises(CyclicDependencyError):
        g.topological_order()


def test_resource_graph_dependencies():
    g = ResourceGraph()
    g.add_edge("fn", "storage")
    assert "storage" in g.dependencies("fn")
    assert g.dependencies("storage") == frozenset()


def test_build_graph_from_app():
    app = App("test-graph")

    @app.storage(read_latency="< 10ms")
    class MyStore:
        pass

    @app.function(compute=Compute(collocate_with="test-graph.MyStore"))
    async def my_fn() -> dict:
        return {}

    g = build_graph(app)
    assert "test-graph.my_fn" in g.nodes
    assert "test-graph.MyStore" in g.nodes


def test_build_graph_no_cycles():
    app = App("no-cycle")

    @app.storage()
    class Store:
        pass

    @app.function()
    async def fn() -> dict:
        return {}

    g = build_graph(app)
    # Should not raise
    order = g.topological_order()
    assert len(order) == 2


# ── explain_plan ──────────────────────────────────────────────────────────────

def _make_plan(storage_backend: str = "redis") -> PlanFile:
    return PlanFile(
        app_name="test",
        version=1,
        previous_version=None,
        deploy_target="generic",
        storage={
            "test.Sessions": StorageSpec(
                variable_name="test.Sessions",
                backend=storage_backend,
                previous_backend=None,
                migration_plan=None,
                migration_stage=0,
                schema_hash="abc123",
                reason=f"selected {storage_backend}; cost=$3.5/GB/mo",
            )
        },
        compute={
            "test.process": ComputeSpec(
                function_name="test.process",
                instance_type="c5-large",
                instances=2,
                previous_instance_type=None,
                reason="cheapest cpu instance",
            )
        },
    )


def test_explain_plan_contains_backend():
    plan = _make_plan("redis")
    text = explain_plan(plan)
    assert "redis" in text
    assert "test.Sessions" in text


def test_explain_plan_contains_compute():
    plan = _make_plan()
    text = explain_plan(plan)
    assert "c5-large" in text
    assert "test.process" in text


def test_explain_plan_rich_mode():
    plan = _make_plan()
    text = explain_plan(plan, rich=True)
    assert "[bold]" in text


def test_explain_plan_empty():
    plan = PlanFile(
        app_name="empty",
        version=1,
        previous_version=None,
        deploy_target="generic",
    )
    text = explain_plan(plan)
    assert "empty" in text


# ── diff_plans / stability ────────────────────────────────────────────────────

def test_diff_plans_stable():
    plan = _make_plan("redis")
    diff = diff_plans(plan, plan)
    assert diff.verdict == StabilityVerdict.STABLE
    assert diff.diffs == []
    assert diff.is_stable


def test_diff_plans_added_storage():
    old = _make_plan("redis")
    new_plan = PlanFile(
        app_name="test",
        version=2,
        previous_version=1,
        deploy_target="generic",
        storage={
            "test.Sessions": old.storage["test.Sessions"],
            "test.NewStore": StorageSpec(
                variable_name="test.NewStore",
                backend="postgres",
                previous_backend=None,
                migration_plan=None,
                migration_stage=0,
                schema_hash="xyz",
                reason="",
            ),
        },
    )
    diff = diff_plans(old, new_plan)
    assert diff.verdict == StabilityVerdict.DRIFT
    changes = {d.change for d in diff.diffs}
    assert "added" in changes


def test_diff_plans_backend_changed():
    old = _make_plan("redis")
    new_plan = _make_plan("dynamodb")
    diff = diff_plans(old, new_plan)
    assert diff.verdict == StabilityVerdict.BREAKING
    assert len(diff.breaking_changes) == 1
    assert diff.breaking_changes[0].requires_migration is True


def test_diff_plans_removed_storage():
    old = _make_plan("redis")
    new_plan = PlanFile(
        app_name="test",
        version=2,
        previous_version=1,
        deploy_target="generic",
        storage={},
    )
    diff = diff_plans(old, new_plan)
    assert diff.verdict == StabilityVerdict.DRIFT
    changes = {d.change for d in diff.diffs}
    assert "removed" in changes


def test_plan_diff_summary():
    old = _make_plan("redis")
    new_plan = _make_plan("dynamodb")
    diff = diff_plans(old, new_plan)
    summary = diff.summary()
    assert "MIGRATION REQUIRED" in summary
    assert "breaking" in summary
