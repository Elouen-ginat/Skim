"""Compute constraint encoding for the Z3 solver.

Mirrors the storage solver in skaal/solver/storage.py but selects
compute instance types from the catalog's ``[compute.*]`` section based on
declared Compute constraints (latency, throughput, compute_type, memory).
"""

from __future__ import annotations

from typing import Any


class UnsatisfiableComputeConstraints(Exception):
    """Raised when no instance type satisfies the declared compute constraints."""

    def __init__(self, function_name: str, reason: str = "") -> None:
        self.function_name = function_name
        self.reason = reason
        super().__init__(
            f"Cannot satisfy compute constraints for {function_name!r}. {reason}"
        )


def encode_compute(
    function_name: str,
    constraints: Any,
    instance_types: dict[str, Any],
    target: str = "generic",
) -> tuple[str, str]:
    """
    Select a compute instance type using Z3 optimization.

    Encodes compute constraints (latency, throughput, compute_type, memory)
    as Z3 Boolean decision variables.  Hard constraints eliminate incompatible
    instances; a cost minimization objective picks the cheapest survivor.

    Args:
        function_name:  Qualified function name, e.g. ``"counter.increment"``.
        constraints:    A :class:`~skaal.types.Compute` instance (or any object
                        with ``compute_type``, ``latency``, ``memory`` attrs).
        instance_types: ``catalog["compute"]`` dict — backend name → spec dict.
        target:         Deploy target: ``"generic"`` | ``"aws-lambda"`` | ``"k8s"``
                        | ``"ecs"``.

    Returns:
        ``(instance_type_name, reason_string)``

    Raises:
        :class:`UnsatisfiableComputeConstraints` if no instance qualifies.
    """
    # ── Fast-path for serverless target ──────────────────────────────────────
    if target == "aws-lambda":
        return "lambda", "serverless Lambda — no persistent compute provisioned"

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
        # memory may be "16GB" string or float
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
            spec_mem = spec.get("memory_gb", 0)
            if spec_mem < memory_gb:
                compatible = False

        # Latency filter: use cost-weighted heuristic (faster = more expensive)
        # Reject instances that have zero vcpus (can't serve latency-sensitive work)
        if latency_ms is not None:
            vcpus = spec.get("vcpus", 1)
            # Very rough heuristic: each vCPU contributes ~50ms of headroom
            estimated_max_ms = 5000.0 / max(vcpus, 1)
            if estimated_max_ms > latency_ms * 10:
                # Instance too slow to plausibly meet the latency
                compatible = False

        if not compatible:
            opt.add(sel[name] == False)  # noqa: E712

    # Minimize cost
    cost_terms = []
    for name, spec in instance_types.items():
        cost = int(spec.get("cost_per_hour", 0) * 1000)
        cost_terms.append(If(sel[name], cost, 0))
    opt.minimize(Sum(cost_terms))

    result = opt.check()
    if result != sat:
        reasons = []
        if compute_type != "cpu":
            reasons.append(f"compute_type={compute_type}")
        if memory_gb is not None:
            reasons.append(f"memory>={memory_gb}GB")
        if latency_ms is not None:
            reasons.append(f"latency<{latency_ms}ms")
        raise UnsatisfiableComputeConstraints(
            function_name,
            f"No instance satisfies: {', '.join(reasons) or 'constraints'}",
        )

    model = opt.model()
    selected = next(
        (n for n in names if model[sel[n]] is not None and str(model[sel[n]]) == "True"),
        None,
    )

    if selected is None:
        raise UnsatisfiableComputeConstraints(function_name, "Z3 returned sat but no instance selected")

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
