"""High-level plan() API — entry point for `skaal plan` CLI command.

Orchestrates catalog loading, solving, graph analysis, stability checking,
and explanation in one call.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from skaal.catalog.loader import load_catalog
from skaal.plan import PlanFile
from skaal.solver.explain import explain_plan
from skaal.solver.graph import build_graph
from skaal.solver.solver import solve
from skaal.solver.stability import PlanDiff, diff_plans

if TYPE_CHECKING:
    from skaal.app import App


def plan(
    app: "App",
    *,
    catalog_path: Path | str | None = None,
    target: str = "generic",
    previous: PlanFile | None = None,
    explain: bool = False,
) -> PlanFile:
    """
    Run the full planning pipeline for *app* and return a :class:`~skaal.plan.PlanFile`.

    Steps:
    1. Load catalog (TOML → raw dict).
    2. Build resource dependency graph and validate for cycles.
    3. Solve storage and compute constraints.
    4. (Optional) diff against *previous* plan and attach migration metadata.
    5. (Optional) print a human-readable explanation to stdout.

    Args:
        app:          The Skaal :class:`~skaal.app.App` to plan for.
        catalog_path: Path to catalog TOML; defaults to ``catalogs/aws.toml``.
        target:       Deploy target — ``"generic"``, ``"aws-lambda"``, ``"k8s"``, ``"ecs"``.
        previous:     A previous :class:`~skaal.plan.PlanFile` to diff against.
        explain:      If ``True``, print solver decisions to stdout.

    Returns:
        A solved :class:`~skaal.plan.PlanFile`.
    """
    catalog: dict[str, Any] = load_catalog(catalog_path, target=target)

    # Validate dependency graph (raises CyclicDependencyError on cycles)
    build_graph(app)

    plan_file = solve(app, catalog, target=target)

    if previous is not None:
        _attach_migration_metadata(plan_file, previous)

    if explain:
        print(explain_plan(plan_file))

    return plan_file


def plan_diff(
    app: "App",
    previous: PlanFile,
    *,
    catalog_path: Path | str | None = None,
    target: str = "generic",
) -> PlanDiff:
    """
    Convenience wrapper: plan + diff against *previous* in one call.

    Returns a :class:`~skaal.solver.stability.PlanDiff` describing changes.
    """
    new_plan = plan(app, catalog_path=catalog_path, target=target)
    return diff_plans(previous, new_plan)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _attach_migration_metadata(new: PlanFile, old: PlanFile) -> None:
    """Mutate *new* in-place: set ``previous_backend`` / ``migration_stage`` fields."""
    for qname, spec in new.storage.items():
        if qname in old.storage:
            old_spec = old.storage[qname]
            if old_spec.backend != spec.backend:
                spec.previous_backend = old_spec.backend
                spec.migration_stage = old_spec.migration_stage
            else:
                spec.migration_stage = old_spec.migration_stage
    new.previous_version = old.version
