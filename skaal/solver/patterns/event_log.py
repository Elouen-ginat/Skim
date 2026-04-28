from __future__ import annotations

import warnings

from skaal.plan import PatternSpec
from skaal.solver._pattern_solvers import (
    PatternSolveContext,
    register_pattern_solver,
    storage_constraints_from_pattern,
)
from skaal.solver.storage import UnsatisfiableConstraints, select_backend


@register_pattern_solver("event-log")
def solve_event_log(ctx: PatternSolveContext) -> PatternSpec:
    pattern_constraints = storage_constraints_from_pattern(ctx.pattern_meta)
    try:
        backend_name, reason = select_backend(
            ctx.qname,
            pattern_constraints,
            ctx.storage_backends,
            target=ctx.target,
        )
    except UnsatisfiableConstraints as exc:
        warnings.warn(
            f"EventLog {ctx.qname!r} could not be solved: {exc}. "
            "No backing store will be provisioned.",
            RuntimeWarning,
            stacklevel=2,
        )
        backend_name, reason = "", str(exc)

    storage_meta = ctx.pattern_meta.get("storage", {})
    return PatternSpec(
        pattern_name=ctx.qname,
        pattern_type="event-log",
        backend=backend_name or None,
        reason=reason,
        config={
            "retention": storage_meta.get("retention"),
            "partitions": storage_meta.get("partitions"),
            "durability": (
                storage_meta.get("durability").value
                if hasattr(storage_meta.get("durability"), "value")
                else storage_meta.get("durability")
            ),
        },
    )
