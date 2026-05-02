"""Tests for ADR 021 — solver UNSAT diagnostics."""

from __future__ import annotations

import pytest

from skaal.errors import UnsatisfiableConstraints
from skaal.solver.diagnostics import (
    build_diagnosis,
    evaluate_compute_candidates,
    evaluate_storage_candidates,
)
from skaal.solver.explain import render_diagnosis
from skaal.solver.storage import select_backend
from skaal.types import Latency, Violation


@pytest.fixture
def storage_catalog() -> dict[str, dict]:
    return {
        "local-map": {
            "display_name": "In-Memory Map",
            "read_latency": {"min": 0.001, "max": 0.1, "unit": "ms"},
            "write_latency": {"min": 0.001, "max": 0.1, "unit": "ms"},
            "durability": ["ephemeral"],
            "access_patterns": ["random-read", "random-write"],
            "storage_kinds": ["kv"],
            "cost_per_gb_month": 0.0,
        },
        "sqlite": {
            "display_name": "SQLite",
            "read_latency": {"min": 0.1, "max": 5.0, "unit": "ms"},
            "write_latency": {"min": 0.5, "max": 20.0, "unit": "ms"},
            "durability": ["persistent", "durable"],
            "access_patterns": ["random-read", "random-write", "transactional"],
            "storage_kinds": ["kv", "relational"],
            "cost_per_gb_month": 0.0,
        },
    }


def test_evaluate_storage_candidates_flags_violations(storage_catalog):
    """Each backend gets a per-constraint check; satisfiers report empty violations."""
    constraints = {
        "read_latency": Latency("< 1ms"),
        "durability": "durable",
        "kind": "kv",
    }
    reports = evaluate_storage_candidates(constraints, storage_catalog)

    by_name = {r.backend_name: r for r in reports}
    # local-map: too-strict durability fails (only ephemeral)
    assert any(v.constraint == "durability" for v in by_name["local-map"].violations)
    # sqlite: read_latency 5ms > 1ms fails
    sqlite_lat = [v for v in by_name["sqlite"].violations if v.constraint == "read_latency"]
    assert len(sqlite_lat) == 1
    assert sqlite_lat[0].slack is not None and sqlite_lat[0].slack > 0


def test_select_backend_unsat_attaches_diagnosis(storage_catalog):
    """An UNSAT solve raises with a populated Diagnosis."""
    with pytest.raises(UnsatisfiableConstraints) as exc:
        select_backend(
            "Profiles",
            {
                "read_latency": Latency("< 1ms"),
                "durability": "durable",
                "kind": "kv",
            },
            storage_catalog,
        )
    diag = exc.value.diagnosis
    assert diag is not None
    assert diag.resource_name == "Profiles"
    assert diag.resource_kind == "storage"
    assert len(diag.candidates) == 2
    # Closest match should be sqlite (single read_latency violation), not local-map
    # (which violates durability — categorical, no relax).
    assert diag.closest is not None
    assert diag.closest.backend_name == "sqlite"


def test_single_relax_suggestion_for_one_numeric_violation(storage_catalog):
    """When the closest match has one numeric violation, suggest the relax."""
    with pytest.raises(UnsatisfiableConstraints) as exc:
        select_backend(
            "Profiles",
            {
                "read_latency": Latency("< 1ms"),
                "durability": "persistent",
                "kind": "kv",
            },
            storage_catalog,
        )
    diag = exc.value.diagnosis
    assert diag.suggestion is not None
    assert diag.suggestion.backend_name == "sqlite"
    assert diag.suggestion.constraint == "read_latency"


def test_categorical_violation_has_no_slack(storage_catalog):
    """Durability/access-pattern mismatches must not produce a slack number."""
    reports = evaluate_storage_candidates(
        {"durability": "durable", "kind": "kv"},
        storage_catalog,
    )
    by_name = {r.backend_name: r for r in reports}
    durability_violations = [v for v in by_name["local-map"].violations if v.constraint == "durability"]
    assert durability_violations
    assert durability_violations[0].slack is None


def test_render_diagnosis_plain_includes_closest_match(storage_catalog):
    with pytest.raises(UnsatisfiableConstraints) as exc:
        select_backend(
            "Profiles",
            {"read_latency": Latency("< 1ms"), "durability": "durable", "kind": "kv"},
            storage_catalog,
        )
    text = render_diagnosis(exc.value.diagnosis, rich=False)
    assert "Profiles" in text
    assert "Closest match" in text
    assert "sqlite" in text
    # Plain mode uses ASCII OK/FAIL, not glyphs.
    assert "✓" not in text
    assert "✗" not in text


def test_render_diagnosis_rich_uses_glyphs(storage_catalog):
    with pytest.raises(UnsatisfiableConstraints) as exc:
        select_backend(
            "Profiles",
            {"read_latency": Latency("< 1ms"), "durability": "durable", "kind": "kv"},
            storage_catalog,
        )
    text = render_diagnosis(exc.value.diagnosis, rich=True)
    assert "[bold]" in text
    assert "[red]" in text


def test_diagnosis_with_no_candidates_is_empty():
    """Empty catalog yields an empty Diagnosis without crashing."""
    diag = build_diagnosis(
        resource_name="X",
        resource_kind="storage",
        requested={},
        candidates=(),
    )
    assert diag.closest is None
    assert diag.suggestion is None
    text = render_diagnosis(diag, rich=False)
    assert "no candidates" in text


def test_unsatisfiable_constraints_exit_code_is_2():
    """Constraint UNSAT distinguishes itself from other errors via exit code 2."""
    exc = UnsatisfiableConstraints("X", "no backend")
    assert exc.exit_code == 2


def test_unsatisfiable_back_compat_aliases():
    """variable_name / function_name still resolve for pre-ADR-021 callers."""
    exc = UnsatisfiableConstraints("Counts")
    assert exc.variable_name == "Counts"
    assert exc.function_name == "Counts"


def test_compute_candidates_evaluation():
    """Compute UNSAT mirrors storage shape."""
    instance_types = {
        "t3-micro": {"display_name": "t3.micro", "vcpus": 1, "memory_gb": 1, "compute_types": ["cpu"], "cost_per_hour": 0.01},
        "c5-xlarge": {"display_name": "c5.xlarge", "vcpus": 4, "memory_gb": 8, "compute_types": ["cpu"], "cost_per_hour": 0.20},
    }
    reports = evaluate_compute_candidates({"memory": 16}, instance_types)
    by_name = {r.backend_name: r for r in reports}
    assert by_name["t3-micro"].violations  # 1 GB < 16 GB requested
    assert by_name["c5-xlarge"].violations  # 8 GB < 16 GB requested
    assert all(v.slack is not None and v.slack < 0 for r in reports for v in r.violations)


def test_violation_dataclass_is_frozen():
    """Violation is a value object — mutating it should raise."""
    v = Violation(constraint="read_latency", requested="< 1ms", offered="≤ 5ms", slack=4.0)
    with pytest.raises(Exception):
        v.constraint = "x"  # type: ignore[misc]
