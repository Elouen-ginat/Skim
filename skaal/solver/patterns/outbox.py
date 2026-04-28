from __future__ import annotations

from skaal.plan import PatternSpec
from skaal.solver._pattern_solvers import (
    PatternSolveContext,
    register_pattern_solver,
    resolve_resource_qname,
)


@register_pattern_solver("outbox")
def solve_outbox(ctx: PatternSolveContext) -> PatternSpec:
    channel_obj = ctx.pattern_meta.get("channel")
    storage_obj = ctx.pattern_meta.get("storage")
    channel_qname = resolve_resource_qname(channel_obj, ctx.all_resources) if channel_obj else None
    storage_qname = resolve_resource_qname(storage_obj, ctx.all_resources) if storage_obj else None

    outbox_backend: str | None = None
    if storage_qname and storage_qname in ctx.storage_specs:
        outbox_backend = ctx.storage_specs[storage_qname].backend

    return PatternSpec(
        pattern_name=ctx.qname,
        pattern_type="outbox",
        backend=outbox_backend,
        reason=(
            f"outbox: writes to {storage_qname!r}, forwards to {channel_qname!r}, "
            f"delivery={ctx.pattern_meta.get('delivery')!r}"
        ),
        config={
            "channel": channel_qname,
            "storage": storage_qname,
            "delivery": ctx.pattern_meta.get("delivery"),
        },
    )
