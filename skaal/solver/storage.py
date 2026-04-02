"""Storage constraint encoding and Z3-based backend selection."""

from __future__ import annotations

from typing import Any


class UnsatisfiableConstraints(Exception):
    """Raised when the Z3 solver cannot satisfy the declared constraints."""

    def __init__(self, variable_name: str, reason: str = "") -> None:
        self.variable_name = variable_name
        self.reason = reason
        super().__init__(
            f"Cannot satisfy constraints for {variable_name!r}. {reason}"
        )


# Backends that require VPC per target family
_VPC_PATTERNS_AWS = ("rds-", "rds_", "elasticache", "memcached")
_VPC_PATTERNS_GCP = ("cloud-sql", "memorystore")


def _requires_vpc(backend_name: str, spec: dict, target: str = "generic") -> bool:
    """True if this backend needs a VPC for the given target, from catalog flag or name heuristics."""
    if spec.get("requires_vpc"):
        return True
    name_lower = backend_name.lower()
    if target in ("aws-lambda", "aws"):
        return any(p in name_lower for p in _VPC_PATTERNS_AWS)
    if target in ("gcp-cloudrun", "gcp"):
        return any(p in name_lower for p in _VPC_PATTERNS_GCP)
    return False


def select_backend(
    variable_name: str,
    constraints: dict[str, Any],
    backends: dict[str, Any],
    target: str = "generic",
) -> tuple[str, str]:
    """
    Use Z3 to select the best backend for a storage variable.

    Args:
        variable_name: e.g. "counter.Counts"
        constraints: __skim_storage__ dict (read_latency, durability, access_pattern, etc.)
        backends: dict of name -> spec from catalog TOML
        target: "generic" | "aws-lambda" | "k8s"

    Returns:
        (backend_name, reason_string)

    Raises:
        UnsatisfiableConstraints if no backend qualifies.
    """
    from z3 import Bool, If, Optimize, Sum, sat

    # Extract constraints
    read_latency = constraints.get("read_latency")
    durability = constraints.get("durability")
    access_pattern = constraints.get("access_pattern")

    # Build Z3 optimizer
    opt = Optimize()

    sel_vars: dict[str, Any] = {}
    backend_names = list(backends.keys())

    for name in backend_names:
        sel_vars[name] = Bool(f"sel_{name}")

    # Exactly-one constraint
    opt.add(Sum([If(sel_vars[n], 1, 0) for n in backend_names]) == 1)

    # Hard constraints — filter incompatible backends
    for name, spec in backends.items():
        var = sel_vars[name]
        compatible = True

        # Check access_pattern
        if access_pattern is not None:
            ap_value = access_pattern.value if hasattr(access_pattern, "value") else str(access_pattern)
            spec_patterns = spec.get("access_patterns", [])
            if ap_value not in spec_patterns:
                compatible = False

        # Check durability
        if durability is not None:
            dur_value = durability.value if hasattr(durability, "value") else str(durability)
            spec_durabilities = spec.get("durability", [])
            if dur_value not in spec_durabilities:
                compatible = False

        # Check read_latency
        if read_latency is not None:
            op = read_latency.op
            ms = read_latency.ms
            spec_latency = spec.get("read_latency", {})
            lat_max = spec_latency.get("max", float("inf"))
            if op in ("<", "<="):
                # Backend's max latency must be <= constraint
                if lat_max > ms:
                    compatible = False

        if not compatible:
            opt.add(var == False)  # noqa: E712

    # Soft objective: minimize cost
    cost_terms = []
    for name, spec in backends.items():
        var = sel_vars[name]
        # Base cost: cost_per_gb_month * 100 as integer
        cost = int(spec.get("cost_per_gb_month", 0) * 100)
        # Add VPC penalty for serverless targets (Lambda, Cloud Run)
        if target in ("aws-lambda", "aws", "gcp-cloudrun", "gcp") and _requires_vpc(
            name, spec, target=target
        ):
            cost += 1000
        cost_terms.append(If(var, cost, 0))

    opt.minimize(Sum(cost_terms))

    result = opt.check()
    if result != sat:
        # Determine reason
        reasons = []
        if read_latency is not None:
            reasons.append(f"read_latency {read_latency.expr}")
        if durability is not None:
            dur_val = durability.value if hasattr(durability, "value") else str(durability)
            reasons.append(f"durability={dur_val}")
        if access_pattern is not None:
            ap_val = access_pattern.value if hasattr(access_pattern, "value") else str(access_pattern)
            reasons.append(f"access_pattern={ap_val}")
        raise UnsatisfiableConstraints(
            variable_name,
            f"No backend satisfies: {', '.join(reasons)}",
        )

    model = opt.model()
    selected = None
    for name in backend_names:
        val = model[sel_vars[name]]
        if val is not None and str(val) == "True":
            selected = name
            break

    if selected is None:
        # Fallback: find any compatible backend
        raise UnsatisfiableConstraints(variable_name, "Z3 returned sat but no backend selected")

    spec = backends[selected]
    # Build reason string
    reason_parts = [f"selected {spec.get('display_name', selected)}"]
    if read_latency is not None:
        lat_max = spec.get("read_latency", {}).get("max", "?")
        reason_parts.append(f"read_latency max={lat_max}ms satisfies {read_latency.expr}")
    if durability is not None:
        dur_val = durability.value if hasattr(durability, "value") else str(durability)
        reason_parts.append(f"durability={dur_val}")
    if target in ("aws-lambda", "aws") and _requires_vpc(selected, spec, target=target):
        reason_parts.append("WARNING: requires VPC in Lambda context")
    elif target in ("aws-lambda", "aws"):
        reason_parts.append("serverless-compatible (no VPC)")
    elif target in ("gcp-cloudrun", "gcp") and _requires_vpc(selected, spec, target=target):
        reason_parts.append("WARNING: requires VPC Connector in Cloud Run context")
    elif target in ("gcp-cloudrun", "gcp"):
        reason_parts.append("serverless-compatible (no VPC)")

    cost = spec.get("cost_per_gb_month", 0)
    reason_parts.append(f"cost=${cost}/GB/mo")

    return selected, "; ".join(reason_parts)
