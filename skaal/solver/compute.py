"""Compute constraint encoding for the Z3 solver.

Mirrors the storage solver in :mod:`skaal.solver.storage` but selects compute
instance types from the catalog's ``[compute.*]`` section based on declared
:class:`~skaal.types.Compute` constraints (latency, throughput, compute_type,
memory).
"""

from __future__ import annotations

from typing import Any

from skaal.errors import UnsatisfiableConstraints
from skaal.solver.targets import catalog_compute_key

# Back-compat alias: pre-ADR-021 callers imported UnsatisfiableComputeConstraints.
# Now unified with the storage variant via skaal.errors.UnsatisfiableConstraints.
UnsatisfiableComputeConstraints = UnsatisfiableConstraints

__all__ = [
    "UnsatisfiableComputeConstraints",
    "UnsatisfiableConstraints",
    "encode_compute",
]


def encode_compute(
    function_name: str,
    constraints: Any,
    instance_types: dict[str, Any],
    target: str = "generic",
) -> tuple[str, str]:
    """Select a compute instance type using Z3 optimization.

    Encodes compute constraints (latency, throughput, compute_type, memory)
    as Z3 Boolean decision variables.  Hard constraints eliminate incompatible
    instances; a cost minimization objective picks the cheapest survivor.

    For serverless targets (AWS Lambda, GCP Cloud Run) no persistent compute
    is provisioned, so the function returns the catalog key immediately without
    running the solver.

    Args:
        function_name:  Qualified function name, e.g. ``"counter.increment"``.
        constraints:    A :class:`~skaal.types.Compute` instance (or any object
                        with ``compute_type``, ``latency``, ``memory`` attrs).
        instance_types: ``catalog["compute"]`` dict — backend name → spec dict.
        target:         Deploy target: ``"generic"`` | ``"aws"`` | ``"k8s"``
                        | ``"ecs"``.

    Returns:
        ``(instance_type_name, reason_string)``

    Raises:
        :class:`UnsatisfiableComputeConstraints` if no instance qualifies.
    """
    # ── Fast-path for serverless targets ─────────────────────────────────────
    # Lambda and Cloud Run manage compute themselves; the solver just records
    # the catalog key so deploy generators can read their deploy config.
    compute_key = catalog_compute_key(target)
    if compute_key is not None:
        return compute_key, f"serverless target={target!r} — no persistent compute provisioned"

    # ── Extract constraint values ─────────────────────────────────────────────
    compute_type = "cpu"
    if hasattr(constraints, "compute_type") and constraints.compute_type is not None:
        ct = constraints.compute_type
        compute_type = ct.value if hasattr(ct, "value") else str(ct)

    latency_ms: float | None = None
    if hasattr(constraints, "latency") and constraints.latency is not None:
        latency_ms = constraints.latency.ms

    memory_gb: float | None = None
    if hasattr(constraints, "memory") and constraints.memory is not None:
        mem = constraints.memory
        if isinstance(mem, str):
            import re

            m = re.match(r"([\d.]+)\s*GB?", mem, re.IGNORECASE)
            memory_gb = float(m.group(1)) if m else None
        else:
            memory_gb = float(mem)

    if not instance_types:
        return "c5-large", "default compute (no catalog entries)"

    # ── Z3 solver ─────────────────────────────────────────────────────────────
    from z3 import Bool, If, Optimize, Sum, sat

    opt = Optimize()
    names = list(instance_types.keys())

    def _z3_var(name: str) -> str:
        return "sel_" + name.replace("-", "_").replace(".", "_")

    sel: dict[str, Any] = {n: Bool(_z3_var(n)) for n in names}

    # Exactly one instance type selected
    opt.add(Sum([If(sel[n], 1, 0) for n in names]) == 1)

    for name, spec in instance_types.items():
        compatible = True

        # Compute type filter
        spec_types = spec.get("compute_types", ["cpu"])
        if compute_type != "any" and compute_type not in spec_types:
            compatible = False

        # Memory filter
        if memory_gb is not None:
            if spec.get("memory_gb", 0) < memory_gb:
                compatible = False

        # Latency filter: use vCPU-count heuristic (faster = more cores)
        # Reject instances where the rough headroom is an order of magnitude
        # below the requested latency bound.
        if latency_ms is not None:
            vcpus = spec.get("vcpus", 1)
            estimated_max_ms = 5000.0 / max(vcpus, 1)
            if estimated_max_ms > latency_ms * 10:
                compatible = False

        if not compatible:
            opt.add(sel[name] == False)  # noqa: E712

    # Minimise cost
    cost_terms = [
        If(sel[n], int(spec.get("cost_per_hour", 0) * 1000), 0)
        for n, spec in instance_types.items()
    ]
    opt.minimize(Sum(cost_terms))

    result = opt.check()
    if result != sat:
        from skaal.solver.diagnostics import build_diagnosis, evaluate_compute_candidates

        diag_constraints: dict[str, Any] = {
            "compute_type": compute_type if compute_type != "cpu" else None,
            "memory": memory_gb,
            "latency": latency_ms,
        }
        requested_text: dict[str, str] = {}
        if compute_type != "cpu":
            requested_text["compute_type"] = f"compute_type={compute_type}"
        if memory_gb is not None:
            requested_text["memory"] = f"memory ≥ {memory_gb} GB"
        if latency_ms is not None:
            requested_text["latency"] = f"latency < {latency_ms}ms"

        diagnosis = build_diagnosis(
            resource_name=function_name,
            resource_kind="compute",
            requested=requested_text,
            candidates=evaluate_compute_candidates(diag_constraints, instance_types),
        )
        reason_summary = ", ".join(requested_text.values()) or "constraints"
        raise UnsatisfiableConstraints(
            function_name,
            f"No instance satisfies: {reason_summary}",
            diagnosis=diagnosis,
        )

    model = opt.model()
    selected = next(
        (n for n in names if model[sel[n]] is not None and str(model[sel[n]]) == "True"),
        None,
    )

    if selected is None:
        raise UnsatisfiableConstraints(function_name, "Z3 returned sat but no instance selected")

    spec = instance_types[selected]
    display = spec.get("display_name", selected)
    cost = spec.get("cost_per_hour", 0)
    parts = [f"selected {display}"]
    if compute_type != "cpu":
        parts.append(f"compute_type={compute_type}")
    if memory_gb is not None:
        parts.append(f"memory={spec.get('memory_gb', '?')}GB")
    parts.append(f"cost=${cost}/hr")
    return selected, "; ".join(parts)
