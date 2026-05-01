"""Storage constraint encoding and Z3-based backend selection."""

from __future__ import annotations

from typing import Any, Callable

from skaal.errors import UnsatisfiableConstraints
from skaal.solver.targets import is_serverless, resolve_family

__all__ = [
    "UnsatisfiableConstraints",
    "select_backend",
]


# ── Internal helpers ──────────────────────────────────────────────────────────


def _enum_value(v: Any) -> str:
    """Normalise an enum member or plain string to its string value."""
    return v.value if hasattr(v, "value") else str(v)


# ── Constraint checker registry ───────────────────────────────────────────────
#
# Each entry maps a constraint key (matching __skaal_storage__ dict keys) to a
# callable ``(value, spec) -> bool`` that returns ``True`` when *spec*
# satisfies *value*.  Adding a new constraint type is a single dict entry.

ConstraintChecker = Callable[[Any, dict[str, Any]], bool]


def _check_access_pattern(value: Any, spec: dict[str, Any]) -> bool:
    return _enum_value(value) in spec.get("access_patterns", [])


def _check_kind(value: Any, spec: dict[str, Any]) -> bool:
    supported = spec.get("storage_kinds", ["kv"])
    return _enum_value(value) in supported


def _check_durability(value: Any, spec: dict[str, Any]) -> bool:
    return _enum_value(value) in spec.get("durability", [])


def _check_read_latency(value: Any, spec: dict[str, Any]) -> bool:
    if value.op not in ("<", "<="):
        return True  # only enforce upper-bound constraints
    return spec.get("read_latency", {}).get("max", float("inf")) <= value.ms


def _check_write_latency(value: Any, spec: dict[str, Any]) -> bool:
    if value.op not in ("<", "<="):
        return True
    return spec.get("write_latency", {}).get("max", float("inf")) <= value.ms


def _check_write_throughput(value: Any, spec: dict[str, Any]) -> bool:
    return spec.get("write_throughput", {}).get("max", 0) >= value


def _check_size_hint(value: Any, spec: dict[str, Any]) -> bool:
    max_size = spec.get("max_size_gb", 0)
    return max_size == 0 or max_size >= value  # 0 means unlimited


def _check_consistency(value: Any, spec: dict[str, Any]) -> bool:
    return _enum_value(value) in spec.get("consistency", [])


def _check_residency(value: Any, spec: dict[str, Any]) -> bool:
    return _enum_value(value) in spec.get("residency", [])


def _check_retention(value: Any, spec: dict[str, Any]) -> bool:
    return _enum_value(value) in spec.get("retention", [])


_CONSTRAINT_CHECKERS: dict[str, ConstraintChecker] = {
    "kind": _check_kind,
    "access_pattern": _check_access_pattern,
    "durability": _check_durability,
    "read_latency": _check_read_latency,
    "write_latency": _check_write_latency,
    "write_throughput": _check_write_throughput,
    "size_hint": _check_size_hint,
    "consistency": _check_consistency,
    "residency": _check_residency,
    "retention": _check_retention,
}


# ── Constraint formatter registry ─────────────────────────────────────────────
#
# Parallel to _CONSTRAINT_CHECKERS: each entry produces a human-readable
# string for a constraint value, used in error messages and reason strings.

ConstraintFormatter = Callable[[Any], str]

_CONSTRAINT_FORMATTERS: dict[str, ConstraintFormatter] = {
    "kind": lambda v: f"kind={_enum_value(v)}",
    "access_pattern": lambda v: f"access_pattern={_enum_value(v)}",
    "durability": lambda v: f"durability={_enum_value(v)}",
    "read_latency": lambda v: f"read_latency {v.expr}",
    "write_latency": lambda v: f"write_latency {v.expr}",
    "write_throughput": lambda v: f"write_throughput={v} ops/sec",
    "size_hint": lambda v: f"size_hint={v} GB",
    "consistency": lambda v: f"consistency={_enum_value(v)}",
    "residency": lambda v: f"residency={_enum_value(v)}",
    "retention": lambda v: f"retention={_enum_value(v)}",
}


def _is_compatible(constraints: dict[str, Any], spec: dict[str, Any]) -> bool:
    """Return ``True`` if *spec* satisfies every non-``None`` constraint."""
    for key, checker in _CONSTRAINT_CHECKERS.items():
        value = constraints.get(key)
        if value is not None and not checker(value, spec):
            return False
    return True


def _build_error_reasons(constraints: dict[str, Any]) -> list[str]:
    """Build a list of human-readable strings for all active constraints."""
    return [
        _CONSTRAINT_FORMATTERS[key](value)
        for key, value in constraints.items()
        if value is not None and key in _CONSTRAINT_FORMATTERS
    ]


def _build_selection_reason(
    selected: str,
    spec: dict[str, Any],
    constraints: dict[str, Any],
    target: str,
) -> str:
    """Compose the reason string reported in the plan for a selected backend."""
    parts: list[str] = [f"selected {spec.get('display_name', selected)}"]

    # Latency satisfaction details
    read_latency = constraints.get("read_latency")
    if read_latency is not None:
        lat_max = spec.get("read_latency", {}).get("max", "?")
        parts.append(f"read_latency max={lat_max}ms satisfies {read_latency.expr}")

    kind = constraints.get("kind")
    if kind is not None:
        parts.append(f"kind={_enum_value(kind)}")

    write_latency = constraints.get("write_latency")
    if write_latency is not None:
        lat_max = spec.get("write_latency", {}).get("max", "?")
        parts.append(f"write_latency max={lat_max}ms satisfies {write_latency.expr}")

    durability = constraints.get("durability")
    if durability is not None:
        parts.append(f"durability={_enum_value(durability)}")

    # Throughput / size / etc.
    write_throughput = constraints.get("write_throughput")
    if write_throughput is not None:
        tp_max = spec.get("write_throughput", {}).get("max", "?")
        parts.append(f"write_throughput max={tp_max} ops/sec satisfies {write_throughput}")

    size_hint = constraints.get("size_hint")
    if size_hint is not None:
        max_sz = spec.get("max_size_gb", "unlimited")
        parts.append(f"max_size_gb={max_sz} satisfies {size_hint} GB request")

    for key in ("consistency", "residency", "retention"):
        value = constraints.get(key)
        if value is not None:
            parts.append(_CONSTRAINT_FORMATTERS[key](value))

    # VPC note — only relevant for serverless targets
    if is_serverless(target):
        family = resolve_family(target).value.upper()
        if spec.get("requires_vpc", False):
            parts.append(f"WARNING: requires VPC in {family} serverless context")
        else:
            parts.append("serverless-compatible (no VPC required)")

    parts.append(f"cost=${spec.get('cost_per_gb_month', 0)}/GB/mo")
    return "; ".join(parts)


# ── Main entry point ──────────────────────────────────────────────────────────


def select_backend(
    variable_name: str,
    constraints: dict[str, Any],
    backends: dict[str, Any],
    target: str = "generic",
) -> tuple[str, str]:
    """Use Z3 to select the best backend for a storage variable.

    Args:
        variable_name: e.g. ``"counter.Counts"``
        constraints:   ``__skaal_storage__`` dict — read_latency, write_latency,
                       durability, access_pattern, etc.
        backends:      ``catalog["storage"]`` dict — backend name → spec dict.
        target:        Deploy target, e.g. ``"generic"``, ``"aws"``, ``"k8s"``.

    Returns:
        ``(backend_name, reason_string)``

    Raises:
        :class:`UnsatisfiableConstraints` if no backend qualifies.
    """
    from z3 import Bool, If, Optimize, Sum, sat

    opt = Optimize()
    backend_names = list(backends.keys())
    sel_vars: dict[str, Any] = {n: Bool(f"sel_{n}") for n in backend_names}

    # Exactly one backend must be selected
    opt.add(Sum([If(sel_vars[n], 1, 0) for n in backend_names]) == 1)

    # Hard constraints — eliminate incompatible backends
    for name, spec in backends.items():
        if not _is_compatible(constraints, spec):
            opt.add(sel_vars[name] == False)  # noqa: E712

    # Soft objective: minimise cost; penalise VPC-requiring backends on
    # serverless targets (Lambda / Cloud Run) to prefer native-serverless options.
    serverless = is_serverless(target)
    cost_terms = []
    for name, spec in backends.items():
        base_cost = int(spec.get("cost_per_gb_month", 0) * 100)
        vpc_penalty = 1000 if serverless and spec.get("requires_vpc", False) else 0
        cost_terms.append(If(sel_vars[name], base_cost + vpc_penalty, 0))
    opt.minimize(Sum(cost_terms))

    result = opt.check()
    if result != sat:
        from skaal.solver.diagnostics import build_diagnosis, evaluate_storage_candidates

        reasons = _build_error_reasons(constraints)
        diagnosis = build_diagnosis(
            resource_name=variable_name,
            resource_kind="storage",
            requested={
                key: _CONSTRAINT_FORMATTERS[key](v)
                for key, v in constraints.items()
                if v is not None and key in _CONSTRAINT_FORMATTERS
            },
            candidates=evaluate_storage_candidates(constraints, backends),
        )
        raise UnsatisfiableConstraints(
            variable_name,
            f"No backend satisfies: {', '.join(reasons) or 'constraints'}",
            diagnosis=diagnosis,
        )

    model = opt.model()
    selected = next(
        (
            n
            for n in backend_names
            if model[sel_vars[n]] is not None and str(model[sel_vars[n]]) == "True"
        ),
        None,
    )

    if selected is None:
        raise UnsatisfiableConstraints(variable_name, "Z3 returned sat but no backend selected")

    spec = backends[selected]
    reason = _build_selection_reason(selected, spec, constraints, target)
    return selected, reason
