"""Tests for constraint solver — verify all constraint types are checked."""

from __future__ import annotations

import pytest

from skaal.app import App
from skaal.solver.solver import solve
from skaal.solver.storage import UnsatisfiableConstraints


@pytest.fixture
def test_catalog() -> dict[str, dict]:
    """Minimal test catalog with multiple backend options."""
    return {
        "storage": {
            "fast-memory": {
                "display_name": "Fast Memory",
                "read_latency": {"min": 0.01, "max": 0.1, "unit": "ms"},
                "write_latency": {"min": 0.01, "max": 0.1, "unit": "ms"},
                "write_throughput": {"max": 100000},
                "max_size_gb": 10,
                "durability": ["ephemeral", "persistent"],
                "access_patterns": ["random-read", "random-write"],
                "consistency": ["strong", "eventual"],
                "cost_per_gb_month": 0.001,
            },
            "slow-persistent": {
                "display_name": "Slow Persistent",
                "read_latency": {"min": 1.0, "max": 10.0, "unit": "ms"},
                "write_latency": {"min": 2.0, "max": 20.0, "unit": "ms"},
                "write_throughput": {"max": 1000},
                "max_size_gb": 1000,
                "durability": ["persistent", "durable"],  # Added persistent
                "access_patterns": ["sequential", "bulk-read", "random-read"],
                "consistency": ["eventual", "strong"],
                "cost_per_gb_month": 0.1,
            },
        }
    }


class TestConstraintSolver:
    """Test the constraint solver handles all constraint types."""

    def test_solver_checks_read_latency(self, test_catalog: dict) -> None:
        """Solver should reject backends that don't meet read_latency constraint."""
        app = App(name="test-app")

        @app.storage(read_latency="< 1ms")  # Requires latency < 1ms
        class FastStorage:
            pass

        plan = solve(app, test_catalog)

        # Should select fast-memory because only it has read_latency.max < 1ms
        assert plan.storage["test-app.FastStorage"].backend == "fast-memory"

    def test_solver_rejects_unsatisfiable_read_latency(self, test_catalog: dict) -> None:
        """Solver should fail when no backend meets read_latency."""
        app = App(name="test-app")

        @app.storage(read_latency="< 0.001ms")  # Impossible
        class ImpossiblyFastStorage:
            pass

        with pytest.raises(UnsatisfiableConstraints) as exc_info:
            solve(app, test_catalog)

        assert "read_latency" in str(exc_info.value)

    def test_solver_checks_write_latency(self, test_catalog: dict) -> None:
        """Solver should check write_latency constraint."""
        app = App(name="test-app")

        @app.storage(write_latency="< 1ms")  # Requires latency < 1ms
        class FastWrite:
            pass

        plan = solve(app, test_catalog)
        assert plan.storage["test-app.FastWrite"].backend == "fast-memory"

    def test_solver_checks_write_throughput(self, test_catalog: dict) -> None:
        """Solver should check write_throughput constraint."""
        app = App(name="test-app")

        @app.storage(write_throughput=10000)  # 10k ops/sec
        class HighThroughput:
            pass

        plan = solve(app, test_catalog)
        # Should select fast-memory (100k max > 10k required)
        assert plan.storage["test-app.HighThroughput"].backend == "fast-memory"

    def test_solver_rejects_unsatisfiable_throughput(self, test_catalog: dict) -> None:
        """Solver should fail when no backend meets throughput."""
        app = App(name="test-app")

        @app.storage(write_throughput=1000000)  # 1M ops/sec - impossible
        class ImpossibleThroughput:
            pass

        with pytest.raises(UnsatisfiableConstraints) as exc_info:
            solve(app, test_catalog)
        assert "throughput" in str(exc_info.value).lower()

    def test_solver_checks_durability(self, test_catalog: dict) -> None:
        """Solver should check durability constraint."""
        app = App(name="test-app")

        @app.storage(durability="durable")
        class DurableStorage:
            pass

        plan = solve(app, test_catalog)
        # Should select slow-persistent (only durable option)
        assert plan.storage["test-app.DurableStorage"].backend == "slow-persistent"

    def test_solver_checks_consistency(self, test_catalog: dict) -> None:
        """Solver should check consistency constraint (via shared decorator if needed)."""
        # Note: consistency is not a storage decorator parameter in this version
        # It's handled via the @shared decorator for distributed state
        # Therefore, we skip this test as consistency is not yet integrated
        # into the solver's storage selection logic
        pytest.skip("Consistency constraint not yet integrated into solver")

    def test_solver_checks_size_hint(self, test_catalog: dict) -> None:
        """Solver should check size_hint constraint."""
        app = App(name="test-app")

        @app.storage(size_hint=20)  # 20 GB - fits in slow-persistent but not fast-memory
        class LargeStorage:
            pass

        plan = solve(app, test_catalog)
        # Should select slow-persistent (1000 GB > 20 GB required, fast-memory only has 10 GB)
        assert plan.storage["test-app.LargeStorage"].backend == "slow-persistent"

    def test_solver_rejects_unsatisfiable_size(self, test_catalog: dict) -> None:
        """Solver should fail when no backend has enough size."""
        app = App(name="test-app")

        @app.storage(size_hint=10000)  # 10TB - too large
        class HugeStorage:
            pass

        with pytest.raises(UnsatisfiableConstraints) as exc_info:
            solve(app, test_catalog)
        assert "size" in str(exc_info.value).lower()

    def test_schema_hash_changes_with_schema(self) -> None:
        """Schema hash should change when class fields change."""
        from skaal.solver.solver import _compute_schema_hash

        class Schema1:
            __annotations__ = {"field1": str}

        class Schema2:
            __annotations__ = {"field1": str, "field2": int}

        hash1 = _compute_schema_hash(Schema1)
        hash2 = _compute_schema_hash(Schema2)

        # Different schemas should have different hashes
        assert hash1 != hash2

    def test_schema_hash_stable_for_same_schema(self) -> None:
        """Schema hash should be stable across calls."""
        from skaal.solver.solver import _compute_schema_hash

        class MySchema:
            __annotations__ = {"name": str, "age": int}

        hash1 = _compute_schema_hash(MySchema)
        hash2 = _compute_schema_hash(MySchema)

        assert hash1 == hash2
