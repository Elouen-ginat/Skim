"""Per-candidate UNSAT diagnostics.

Builds a :class:`~skaal.types.solver.Diagnosis` from a constraint set and a
catalog by evaluating each candidate against each active constraint.  Reuses
the storage solver's ``_CONSTRAINT_CHECKERS`` / ``_CONSTRAINT_FORMATTERS``
registries so there is one source of truth for "does spec X satisfy
constraint Y?".

The diagnostic is purely a presentation concern — the Z3 selection logic
itself is unchanged.
"""

from __future__ import annotations

from typing import Any

from skaal.types.solver import (
    CandidateReport,
    Diagnosis,
    RelaxSuggestion,
    ResourceKind,
    Violation,
)

# ── Slack rules ───────────────────────────────────────────────────────────────


def _latency_slack(value: Any, spec: dict[str, Any], key: str) -> tuple[str | None, float | None]:
    """Return (offered_text, slack_ms) for a Latency-shaped constraint."""
    block = spec.get(key)
    if not isinstance(block, dict) or "max" not in block:
        return None, None
    offered_max = float(block["max"])
    op = getattr(value, "op", "<")
    requested_ms = float(getattr(value, "ms", 0.0))
    if op in ("<", "<="):
        slack = offered_max - requested_ms
        return f"≤ {offered_max}ms", slack
    return f"≤ {offered_max}ms", None


def _throughput_slack(value: Any, spec: dict[str, Any]) -> tuple[str | None, float | None]:
    block = spec.get("write_throughput")
    if not isinstance(block, dict) or "max" not in block:
        return None, None
    offered_max = float(block["max"])
    requested = float(value)
    return f"≤ {offered_max} ops/s", offered_max - requested


def _size_slack(value: Any, spec: dict[str, Any]) -> tuple[str | None, float | None]:
    max_size = spec.get("max_size_gb", 0)
    requested = float(value)
    if max_size == 0:
        return "unlimited", float("inf")
    offered = float(max_size)
    return f"≤ {offered} GB", offered - requested


def _categorical_offered(spec: dict[str, Any], spec_key: str) -> str:
    values = spec.get(spec_key, [])
    if not values:
        return "(not declared)"
    return ", ".join(values)


# Map constraint-key → (offered_text, slack) extractor.
_OfferedFn = Any  # Callable[[Any, dict], tuple[str | None, float | None]]
_SLACK_RULES: dict[str, _OfferedFn] = {
    "read_latency": lambda v, s: _latency_slack(v, s, "read_latency"),
    "write_latency": lambda v, s: _latency_slack(v, s, "write_latency"),
    "write_throughput": _throughput_slack,
    "size_hint": _size_slack,
    "kind": lambda v, s: (_categorical_offered(s, "storage_kinds"), None),
    "access_pattern": lambda v, s: (_categorical_offered(s, "access_patterns"), None),
    "durability": lambda v, s: (_categorical_offered(s, "durability"), None),
    "consistency": lambda v, s: (_categorical_offered(s, "consistency"), None),
    "residency": lambda v, s: (_categorical_offered(s, "residency"), None),
    "retention": lambda v, s: (_categorical_offered(s, "retention"), None),
}


# ── Compute slack rules (mirrors compute.py constraint shape) ─────────────────


def _compute_offered(
    constraint: str, value: Any, spec: dict[str, Any]
) -> tuple[str | None, float | None]:
    if constraint == "compute_type":
        return ", ".join(spec.get("compute_types", ["cpu"])), None
    if constraint == "memory":
        offered = float(spec.get("memory_gb", 0))
        return f"{offered} GB", offered - float(value)
    if constraint == "latency":
        vcpus = max(int(spec.get("vcpus", 1)), 1)
        offered = 5000.0 / vcpus
        # Compute solver rejects when offered > requested * 10 (see compute.py).
        return f"~{offered:.0f}ms (vcpus={vcpus})", float(value) - offered / 10.0
    return None, None


# ── Builders ──────────────────────────────────────────────────────────────────


def evaluate_storage_candidates(
    constraints: dict[str, Any],
    backends: dict[str, dict[str, Any]],
) -> tuple[CandidateReport, ...]:
    """Evaluate every backend against every active constraint."""
    from skaal.solver.storage import _CONSTRAINT_CHECKERS, _CONSTRAINT_FORMATTERS

    reports: list[CandidateReport] = []
    for name, spec in backends.items():
        violations: list[Violation] = []
        for key, value in constraints.items():
            if value is None or key not in _CONSTRAINT_CHECKERS:
                continue
            if _CONSTRAINT_CHECKERS[key](value, spec):
                continue
            offered, slack = _SLACK_RULES.get(key, lambda v, s: (None, None))(value, spec)
            requested = _CONSTRAINT_FORMATTERS[key](value)
            violations.append(Violation(key, requested, offered, slack))
        reports.append(
            CandidateReport(
                backend_name=name,
                display_name=spec.get("display_name", name),
                violations=tuple(violations),
                cost=float(spec.get("cost_per_gb_month", 0.0)),
            )
        )
    return tuple(reports)


def evaluate_compute_candidates(
    constraints: dict[str, Any],
    instance_types: dict[str, dict[str, Any]],
) -> tuple[CandidateReport, ...]:
    """Evaluate every compute instance type against active compute constraints.

    The compute solver rejects on three rules (see ``skaal/solver/compute.py``):
    ``compute_type`` membership, ``memory_gb >= request``, and a vCPU-derived
    latency heuristic.  Mirrored here so the diagnostic and the rejection
    decision agree.
    """
    reports: list[CandidateReport] = []
    for name, spec in instance_types.items():
        violations: list[Violation] = []

        ct = constraints.get("compute_type")
        if ct is not None and ct != "any":
            if ct not in spec.get("compute_types", ["cpu"]):
                offered, _ = _compute_offered("compute_type", ct, spec)
                violations.append(Violation("compute_type", str(ct), offered, None))

        mem = constraints.get("memory")
        if mem is not None and float(spec.get("memory_gb", 0)) < float(mem):
            offered, slack = _compute_offered("memory", mem, spec)
            violations.append(Violation("memory", f"{mem} GB", offered, slack))

        lat = constraints.get("latency")
        if lat is not None:
            vcpus = max(int(spec.get("vcpus", 1)), 1)
            estimated_max_ms = 5000.0 / vcpus
            if estimated_max_ms > float(lat) * 10.0:
                offered, slack = _compute_offered("latency", lat, spec)
                violations.append(Violation("latency", f"< {lat}ms", offered, slack))

        reports.append(
            CandidateReport(
                backend_name=name,
                display_name=spec.get("display_name", name),
                violations=tuple(violations),
                cost=float(spec.get("cost_per_hour", 0.0)),
            )
        )
    return tuple(reports)


# ── Ranking and suggestion ────────────────────────────────────────────────────


_CATEGORICAL_PENALTY = 1e9
"""Sentinel used for categorical (no-slack) violations so they outrank any
relaxable numeric one — relaxable violations can produce a single-relax
suggestion, categorical ones cannot."""


def _rank_key(c: CandidateReport) -> tuple[int, int, float, float]:
    """Order: fewest violations → fewer non-relaxable → smallest slack → cheapest."""
    non_relaxable = sum(1 for v in c.violations if v.slack is None)
    weighted = 0.0
    for v in c.violations:
        if v.slack is None:
            weighted += _CATEGORICAL_PENALTY
        elif v.slack == float("inf"):
            continue
        else:
            weighted += abs(v.slack)
    return (len(c.violations), non_relaxable, weighted, c.cost)


def _build_suggestion(closest: CandidateReport) -> RelaxSuggestion | None:
    """Single-relax hint: if the closest match has exactly one numeric violation,
    suggest relaxing that constraint to the offered value."""
    if len(closest.violations) != 1:
        return None
    v = closest.violations[0]
    if v.slack is None or v.offered is None:
        return None
    return RelaxSuggestion(
        backend_name=closest.backend_name,
        constraint=v.constraint,
        requested=v.requested,
        offered=v.offered,
    )


def build_diagnosis(
    *,
    resource_name: str,
    resource_kind: ResourceKind,
    requested: dict[str, str],
    candidates: tuple[CandidateReport, ...],
    extra_notes: tuple[str, ...] = (),
) -> Diagnosis:
    """Assemble the full :class:`Diagnosis` from evaluated candidates."""
    if not candidates:
        return Diagnosis(
            resource_name=resource_name,
            resource_kind=resource_kind,
            requested=requested,
            candidates=(),
            closest=None,
            suggestion=None,
            extra_notes=extra_notes,
        )
    ranked = sorted(candidates, key=_rank_key)
    closest = ranked[0]
    suggestion = _build_suggestion(closest) if closest.violations else None
    return Diagnosis(
        resource_name=resource_name,
        resource_kind=resource_kind,
        requested=requested,
        candidates=tuple(ranked),
        closest=closest,
        suggestion=suggestion,
        extra_notes=extra_notes,
    )


__all__ = [
    "build_diagnosis",
    "evaluate_compute_candidates",
    "evaluate_storage_candidates",
]
