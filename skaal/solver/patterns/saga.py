from __future__ import annotations

import warnings

from skaal.plan import PatternSpec
from skaal.solver._pattern_solvers import PatternSolveContext, register_pattern_solver


@register_pattern_solver("saga")
def solve_saga(ctx: PatternSolveContext) -> PatternSpec:
    steps = ctx.pattern_meta.get("steps", [])
    missing: list[str] = []
    for step in steps:
        fn_name = step.get("function")
        comp_name = step.get("compensate")
        if fn_name and fn_name not in ctx.registered_functions:
            missing.append(f"function={fn_name!r}")
        if comp_name and comp_name not in ctx.registered_functions:
            missing.append(f"compensate={comp_name!r}")
    if missing:
        warnings.warn(
            f"Saga {ctx.qname!r} references unregistered names: {', '.join(missing)}. "
            "Register them via @app.function before deploying.",
            RuntimeWarning,
            stacklevel=2,
        )

    return PatternSpec(
        pattern_name=ctx.qname,
        pattern_type="saga",
        backend=None,
        reason=(
            f"saga {ctx.pattern_meta.get('name')!r}: {len(steps)} step(s), "
            f"coordination={ctx.pattern_meta.get('coordination')!r}"
        ),
        config={
            "name": ctx.pattern_meta.get("name"),
            "steps": steps,
            "coordination": ctx.pattern_meta.get("coordination"),
            "timeout_ms": ctx.pattern_meta.get("timeout_ms"),
            "missing_references": missing,
        },
    )