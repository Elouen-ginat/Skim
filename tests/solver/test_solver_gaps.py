"""Tests for solver features added to close the library gaps.

Covers:
- ``collocate_with`` propagation into StorageSpec/ComputeSpec and the
  ``resource_order`` topological order.
- ``@scale`` decorator → ComputeSpec.scale_strategy / instances.
- Resilience policies (retry, circuit-breaker, rate-limit, bulkhead) exported
  as JSON-safe dicts on ComputeSpec.
- ``auto_optimize=True`` flag surfaced on StorageSpec.
- Pattern solver (EventLog, Projection, Saga, Outbox) → PatternSpec entries.
- Saga reference validation raises a warning for unknown functions.
"""

from __future__ import annotations

import warnings

import pytest

from skaal import App
from skaal.catalog.loader import load_catalog
from skaal.decorators import scale
from skaal.patterns import EventLog, Outbox, Projection, Saga, SagaStep
from skaal.solver.solver import solve
from skaal.types import (
    Bulkhead,
    CircuitBreaker,
    Compute,
    RateLimitPolicy,
    RetryPolicy,
    Scale,
    ScaleStrategy,
)


# ── collocate_with ────────────────────────────────────────────────────────────


def test_solve_storage_collocate_with_emitted():
    """Storage collocate_with appears on StorageSpec."""
    app = App("colo")

    @app.storage(read_latency="< 10ms")
    class Primary:
        pass

    @app.storage(read_latency="< 10ms", collocate_with="colo.Primary")
    class Secondary:
        pass

    plan = solve(app, load_catalog())
    assert plan.storage["colo.Secondary"].collocate_with == "colo.Primary"
    assert plan.storage["colo.Primary"].collocate_with is None


def test_solve_storage_collocate_with_bare_name():
    """Bare class name in collocate_with resolves to the qualified name."""
    app = App("colo")

    @app.storage(read_latency="< 10ms")
    class Primary:
        pass

    # User writes just "Primary" without the "colo." namespace
    @app.storage(read_latency="< 10ms", collocate_with="Primary")
    class Secondary:
        pass

    plan = solve(app, load_catalog())
    assert plan.storage["colo.Secondary"].collocate_with == "colo.Primary"


def test_solve_compute_collocate_with_emitted():
    """Function collocate_with appears on ComputeSpec."""
    app = App("colo")

    @app.storage(read_latency="< 10ms")
    class Store:
        pass

    @app.function(compute=Compute(collocate_with="colo.Store"))
    async def worker() -> dict:
        return {}

    plan = solve(app, load_catalog())
    assert plan.compute["colo.worker"].collocate_with == "colo.Store"


def test_solve_unknown_collocate_target_warns():
    """Unknown collocate_with target emits a runtime warning."""
    app = App("colo")

    @app.storage(read_latency="< 10ms", collocate_with="does_not_exist")
    class Store:
        pass

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        plan = solve(app, load_catalog())

    assert any("does_not_exist" in str(w.message) for w in caught)
    assert plan.storage["colo.Store"].collocate_with is None


def test_solve_emits_resource_order():
    """PlanFile.resource_order is a topological list (deps before dependents)."""
    app = App("ord")

    @app.storage(read_latency="< 10ms")
    class Store:
        pass

    @app.function(compute=Compute(collocate_with="ord.Store"))
    async def fn() -> dict:
        return {}

    plan = solve(app, load_catalog())
    assert "ord.Store" in plan.resource_order
    assert "ord.fn" in plan.resource_order
    # Store (the dependency) must appear before fn (the dependent)
    assert plan.resource_order.index("ord.Store") < plan.resource_order.index("ord.fn")


def test_solve_propagates_collocation_chain():
    """A → B → C chain flattens so A.collocate_with == C."""
    app = App("chain")

    @app.storage(read_latency="< 10ms")
    class C:
        pass

    @app.storage(read_latency="< 10ms", collocate_with="chain.C")
    class B:
        pass

    @app.storage(read_latency="< 10ms", collocate_with="chain.B")
    class A:
        pass

    plan = solve(app, load_catalog())
    # A → B → C should flatten so A points directly at C
    assert plan.storage["chain.A"].collocate_with == "chain.C"
    assert plan.storage["chain.B"].collocate_with == "chain.C"


# ── @scale decorator ──────────────────────────────────────────────────────────


def test_solve_scale_strategy_emitted():
    """@scale(...) populates ComputeSpec.scale_strategy and instances."""
    app = App("sc")

    @app.function(scale=Scale(instances=4, strategy=ScaleStrategy.PARTITION_BY_KEY))
    async def handler() -> dict:
        return {}

    plan = solve(app, load_catalog())
    spec = plan.compute["sc.handler"]
    assert spec.scale_strategy == "partition-by-key"
    assert spec.instances == 4


def test_solve_scale_decorator_standalone():
    """Bare @scale decorator also populates ComputeSpec."""
    app = App("sc2")

    @app.function
    @scale(instances="auto", strategy=ScaleStrategy.BROADCAST)
    async def handler() -> dict:
        return {}

    plan = solve(app, load_catalog())
    spec = plan.compute["sc2.handler"]
    assert spec.scale_strategy == "broadcast"


# ── Resilience policies ───────────────────────────────────────────────────────


def test_solve_exports_retry_policy():
    app = App("res")

    @app.function(retry=RetryPolicy(max_attempts=5, base_delay_ms=200))
    async def fn() -> dict:
        return {}

    plan = solve(app, load_catalog())
    retry = plan.compute["res.fn"].retry
    assert retry is not None
    assert retry["max_attempts"] == 5
    assert retry["base_delay_ms"] == 200


def test_solve_exports_all_resilience_policies():
    app = App("res")

    @app.function(
        retry=RetryPolicy(max_attempts=3),
        circuit_breaker=CircuitBreaker(failure_threshold=10),
        rate_limit=RateLimitPolicy(requests_per_second=100),
        bulkhead=Bulkhead(max_concurrent_calls=5),
    )
    async def fn() -> dict:
        return {}

    plan = solve(app, load_catalog())
    spec = plan.compute["res.fn"]
    assert spec.retry is not None
    assert spec.circuit_breaker is not None
    assert spec.circuit_breaker["failure_threshold"] == 10
    assert spec.rate_limit is not None
    assert spec.rate_limit["requests_per_second"] == 100
    assert spec.bulkhead is not None
    assert spec.bulkhead["max_concurrent_calls"] == 5


def test_solve_no_resilience_policies_means_none():
    app = App("res")

    @app.function()
    async def fn() -> dict:
        return {}

    plan = solve(app, load_catalog())
    spec = plan.compute["res.fn"]
    assert spec.retry is None
    assert spec.circuit_breaker is None
    assert spec.rate_limit is None
    assert spec.bulkhead is None


# ── auto_optimize ─────────────────────────────────────────────────────────────


def test_solve_auto_optimize_flag_propagated():
    app = App("opt")

    @app.storage(read_latency="< 10ms", auto_optimize=True)
    class S:
        pass

    plan = solve(app, load_catalog())
    assert plan.storage["opt.S"].auto_optimize is True


def test_solve_auto_optimize_default_false():
    app = App("opt")

    @app.storage(read_latency="< 10ms")
    class S:
        pass

    plan = solve(app, load_catalog())
    assert plan.storage["opt.S"].auto_optimize is False


# ── Pattern: EventLog ─────────────────────────────────────────────────────────


def test_solve_eventlog_pattern_provisions_backend():
    app = App("evt")

    Events = EventLog(retention="7d", partitions=4)
    app.pattern(Events)

    plan = solve(app, load_catalog())
    # The pattern key is EventLog (its class name) — patterns are registered by class
    pattern_keys = [k for k in plan.patterns if "EventLog" in k]
    assert pattern_keys, f"no EventLog pattern in {list(plan.patterns)}"
    spec = plan.patterns[pattern_keys[0]]
    assert spec.pattern_type == "event-log"
    # Backend should be something catalog-resolvable (e.g. kafka, s3, kinesis)
    assert spec.backend  # non-empty


# ── Pattern: Projection ──────────────────────────────────────────────────────


def test_solve_projection_pattern_validates_handler():
    app = App("proj")

    @app.storage(read_latency="< 10ms")
    class View:
        pass

    Events = EventLog()
    app.pattern(Events)

    @app.function()
    async def apply_event() -> dict:
        return {}

    view_proj = Projection(source=Events, target=View, handler="apply_event")
    app.pattern(view_proj)

    plan = solve(app, load_catalog())
    proj_keys = [k for k in plan.patterns if "Projection" in k]
    assert proj_keys
    spec = plan.patterns[proj_keys[0]]
    assert spec.pattern_type == "projection"
    assert spec.config["handler"] == "apply_event"
    assert spec.config["target"] == "proj.View"


def test_solve_projection_unknown_handler_warns():
    app = App("proj")

    @app.storage(read_latency="< 10ms")
    class View:
        pass

    Events = EventLog()
    app.pattern(Events)

    view_proj = Projection(source=Events, target=View, handler="missing_function")
    app.pattern(view_proj)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        solve(app, load_catalog())

    assert any("missing_function" in str(w.message) for w in caught)


def test_solve_projection_forces_target_collocation():
    """Projection rewrites the target storage's collocate_with to the source."""
    app = App("proj")

    @app.storage(read_latency="< 10ms")
    class View:
        pass

    Events = EventLog()
    app.pattern(Events)

    @app.function()
    async def apply_event() -> dict:
        return {}

    app.pattern(Projection(source=Events, target=View, handler="apply_event"))

    plan = solve(app, load_catalog())
    # The View target is co-located with the EventLog source
    assert plan.storage["proj.View"].collocate_with is not None
    assert "EventLog" in plan.storage["proj.View"].collocate_with


# ── Pattern: Saga ─────────────────────────────────────────────────────────────


def test_solve_saga_validates_function_references():
    app = App("saga")

    @app.function()
    async def reserve_inventory() -> dict:
        return {}

    @app.function()
    async def release_inventory() -> dict:
        return {}

    placeorder = Saga(
        name="place_order",
        steps=[SagaStep("reserve_inventory", compensate="release_inventory")],
    )
    app.pattern(placeorder)

    # No warning expected — both names are registered
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        plan = solve(app, load_catalog())

    saga_refs = [w for w in caught if "Saga" in str(w.message)]
    assert not saga_refs, f"unexpected saga warnings: {saga_refs}"

    # Saga registers by its .name attribute ("place_order")
    saga_keys = [k for k in plan.patterns if "place_order" in k]
    assert saga_keys, f"no saga pattern in {list(plan.patterns)}"
    spec = plan.patterns[saga_keys[0]]
    assert spec.pattern_type == "saga"
    assert spec.config["name"] == "place_order"
    assert not spec.config["missing_references"]


def test_solve_saga_missing_reference_warns():
    app = App("saga")

    @app.function()
    async def reserve_inventory() -> dict:
        return {}

    # Compensation function is NOT registered
    placeorder = Saga(
        name="place_order",
        steps=[SagaStep("reserve_inventory", compensate="release_inventory")],
    )
    app.pattern(placeorder)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        plan = solve(app, load_catalog())

    saga_warnings = [w for w in caught if "release_inventory" in str(w.message)]
    assert saga_warnings, "expected a warning about the missing compensate function"

    saga_keys = [k for k in plan.patterns if "place_order" in k]
    assert saga_keys, f"no saga pattern in {list(plan.patterns)}"
    spec = plan.patterns[saga_keys[0]]
    assert any("release_inventory" in r for r in spec.config["missing_references"])


# ── Pattern: Outbox ───────────────────────────────────────────────────────────


def test_solve_outbox_borrows_primary_storage_backend():
    """Outbox pattern's backend matches the primary storage it wraps."""
    from skaal.channel import Channel

    app = App("ob")

    @app.storage(read_latency="< 10ms", durability="persistent")
    class Orders:
        pass

    @app.channel()
    class OrderEvents(Channel[dict]):
        pass

    outbox = Outbox(channel=OrderEvents, storage=Orders)
    app.pattern(outbox)

    plan = solve(app, load_catalog())
    ob_keys = [k for k in plan.patterns if "Outbox" in k]
    assert ob_keys
    spec = plan.patterns[ob_keys[0]]
    assert spec.pattern_type == "outbox"
    # Outbox backend should match the primary storage backend
    assert spec.backend == plan.storage["ob.Orders"].backend
    assert spec.config["storage"] == "ob.Orders"
