from __future__ import annotations

import warnings

from skaal.plan import PatternSpec
from skaal.solver._pattern_solvers import (
    PatternSolveContext,
    register_pattern_solver,
    resolve_resource_qname,
)


@register_pattern_solver("projection")
def solve_projection(ctx: PatternSolveContext) -> PatternSpec:
    source = ctx.pattern_meta.get("source")
    target_obj = ctx.pattern_meta.get("target")
    handler = ctx.pattern_meta.get("handler")

    source_qname = resolve_resource_qname(source, ctx.all_resources) if source else None
    target_qname = resolve_resource_qname(target_obj, ctx.all_resources) if target_obj else None

    if handler and handler not in ctx.registered_functions:
        warnings.warn(
            f"Projection {ctx.qname!r} references unknown handler {handler!r}. "
            "Make sure it is registered via @app.function.",
            RuntimeWarning,
            stacklevel=2,
        )

    if target_qname and source_qname and target_qname in ctx.storage_specs:
        existing = ctx.storage_specs[target_qname]
        ctx.storage_specs[target_qname] = existing.model_copy(
            update={"collocate_with": source_qname}
        )

    consistency = ctx.pattern_meta.get("consistency")
    return PatternSpec(
        pattern_name=ctx.qname,
        pattern_type="projection",
        backend=None,
        reason=(
            f"projection {ctx.qname!r}: {source_qname!r} → {target_qname!r} "
            f"via handler={handler!r}"
        ),
        config={
            "source": source_qname,
            "target": target_qname,
            "handler": handler,
            "consistency": (
                consistency.value
                if consistency is not None and hasattr(consistency, "value")
                else consistency
            ),
            "checkpoint_every": ctx.pattern_meta.get("checkpoint_every"),
            "strict": ctx.pattern_meta.get("strict", False),
        },
    )
