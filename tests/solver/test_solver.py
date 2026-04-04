"""Tests for the Z3 constraint solver — storage backend selection."""

from __future__ import annotations

import pytest

from skaal import App
from skaal.catalog.loader import load_catalog
from skaal.solver.solver import solve
from skaal.solver.storage import UnsatisfiableConstraints, select_backend
from skaal.types import AccessPattern, Durability, Latency

# ── Catalog loading ────────────────────────────────────────────────────────────


def test_load_catalog():
    """load_catalog() successfully loads catalogs/aws.toml."""
    catalog = load_catalog()
    assert "storage" in catalog
    assert "compute" in catalog
    # Check expected backends are present
    storage = catalog["storage"]
    assert "dynamodb" in storage
    assert "elasticache-redis" in storage
    assert "elasticache-memcached" in storage
    assert "rds-postgres" in storage
    assert "s3" in storage


# ── select_backend() ──────────────────────────────────────────────────────────


def test_select_backend_ephemeral_low_latency():
    """Ephemeral + < 5ms read_latency + random-read → elasticache-memcached or elasticache-redis."""
    catalog = load_catalog()
    storage_backends = catalog["storage"]

    backend_name, reason = select_backend(
        "Counts",
        {
            "read_latency": Latency("< 5ms"),
            "durability": Durability.EPHEMERAL,
            "access_pattern": AccessPattern.RANDOM_READ,
        },
        storage_backends,
    )
    # Both Redis and Memcached satisfy < 5ms and ephemeral + random-read
    # Solver should pick one; Memcached is cheaper so it's the likely winner
    assert backend_name in (
        "elasticache-memcached",
        "elasticache-redis",
    ), f"Expected a fast cache backend, got {backend_name!r}. Reason: {reason}"


def test_select_backend_durable_lambda():
    """Durable + < 10ms + random-read + aws-lambda target → dynamodb."""
    catalog = load_catalog()
    storage_backends = catalog["storage"]

    backend_name, reason = select_backend(
        "Todos",
        {
            "read_latency": Latency("< 10ms"),
            "durability": Durability.DURABLE,
            "access_pattern": AccessPattern.RANDOM_READ,
        },
        storage_backends,
        target="aws-lambda",
    )
    # DynamoDB: durable, max 10ms, random-read, no VPC → best for Lambda
    assert (
        backend_name == "dynamodb"
    ), f"Expected 'dynamodb' for Lambda target, got {backend_name!r}. Reason: {reason}"
    assert "serverless-compatible" in reason or "no VPC" in reason


def test_select_backend_unsatisfiable():
    """Impossible constraint (< 0.01ms) raises UnsatisfiableConstraints."""
    catalog = load_catalog()
    storage_backends = catalog["storage"]

    with pytest.raises(UnsatisfiableConstraints):
        select_backend(
            "Impossible",
            {
                "read_latency": Latency("< 0.01ms"),
                "durability": Durability.PERSISTENT,
                "access_pattern": AccessPattern.RANDOM_READ,
            },
            storage_backends,
        )


def test_select_backend_unsatisfiable_message():
    """UnsatisfiableConstraints carries variable_name and readable message."""
    catalog = load_catalog()
    storage_backends = catalog["storage"]

    try:
        select_backend(
            "MyVar",
            {"read_latency": Latency("< 0.01ms")},
            storage_backends,
        )
        pytest.fail("Expected UnsatisfiableConstraints")
    except UnsatisfiableConstraints as exc:
        assert exc.variable_name == "MyVar"
        assert "MyVar" in str(exc)


def test_select_backend_append_only_durable():
    """append-only + durable → s3 or msk-kafka."""
    catalog = load_catalog()
    storage_backends = catalog["storage"]

    backend_name, reason = select_backend(
        "EventLog",
        {
            "durability": Durability.DURABLE,
            "access_pattern": AccessPattern.APPEND_ONLY,
        },
        storage_backends,
    )
    assert backend_name in ("s3", "msk-kafka"), f"Expected s3 or msk-kafka, got {backend_name!r}"


def test_select_backend_reason_string():
    """select_backend() returns a non-empty reason string."""
    catalog = load_catalog()
    storage_backends = catalog["storage"]

    _name, reason = select_backend(
        "Data",
        {"durability": Durability.DURABLE, "access_pattern": AccessPattern.RANDOM_READ},
        storage_backends,
    )
    assert isinstance(reason, str) and len(reason) > 0


# ── solve() ───────────────────────────────────────────────────────────────────


def _make_counter_app() -> App:
    app = App("test-counter")

    @app.storage(read_latency="< 5ms", durability="ephemeral")
    class Counts:
        pass

    @app.function()
    async def increment(name: str, by: int = 1) -> dict:
        current = await Counts.get(name) or 0
        await Counts.set(name, current + by)
        return {"name": name, "value": current + by}

    return app


def test_solve_returns_plan_file():
    """solve() returns a valid PlanFile with storage entries."""
    from skaal.plan import PlanFile

    catalog = load_catalog()
    app = _make_counter_app()
    plan = solve(app, catalog)

    assert isinstance(plan, PlanFile)
    assert plan.app_name == "test-counter"
    assert plan.version == 1
    assert plan.previous_version is None


def test_solve_storage_entries():
    """solve() populates PlanFile.storage with correct keys."""
    catalog = load_catalog()
    app = _make_counter_app()
    plan = solve(app, catalog)

    assert len(plan.storage) > 0
    for qname, spec in plan.storage.items():
        assert spec.backend  # non-empty backend name
        assert spec.reason  # non-empty reason
        assert spec.schema_hash  # non-empty hash
        assert spec.migration_stage == 0


def test_solve_compute_entries():
    """solve() populates PlanFile.compute for registered functions."""
    catalog = load_catalog()
    app = _make_counter_app()
    plan = solve(app, catalog)

    assert len(plan.compute) > 0
    for qname, spec in plan.compute.items():
        assert spec.instance_type
        assert spec.reason


def test_solve_lambda_target():
    """solve() with target='aws-lambda' picks Lambda for compute."""
    catalog = load_catalog()
    app = _make_counter_app()
    plan = solve(app, catalog, target="aws-lambda")

    assert plan.deploy_target == "aws-lambda"
    for spec in plan.compute.values():
        assert spec.instance_type == "lambda"


def test_solve_serialization_roundtrip(tmp_path):
    """PlanFile from solve() can be written and re-read."""
    from skaal.plan import PlanFile

    catalog = load_catalog()
    app = _make_counter_app()
    plan = solve(app, catalog)

    out_path = tmp_path / "plan.skaal.lock"
    plan.write(out_path)

    loaded = PlanFile.read(out_path)
    assert loaded.app_name == plan.app_name
    assert set(loaded.storage.keys()) == set(plan.storage.keys())
    assert set(loaded.compute.keys()) == set(plan.compute.keys())
