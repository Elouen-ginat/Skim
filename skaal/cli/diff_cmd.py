"""`skaal diff` — show infrastructure changes between plan versions."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import typer

from skaal.cli._errors import cli_error_boundary

if TYPE_CHECKING:
    from skaal.api import PlanDiff
    from skaal.plan import PlanFile

app = typer.Typer(help="Show what changes between plan versions.")
log = logging.getLogger("skaal.cli")

_PLAN_DEFAULT = "plan.skaal.lock"


def _print_plan_summary(plan: "PlanFile") -> None:
    """Pretty-print a plan file without a second plan to diff against."""
    log.info("Plan: %s  (version %s, target=%s)", plan.app_name, plan.version, plan.deploy_target)
    log.info("")

    if plan.storage:
        log.info("Storage:")
        for name, spec in sorted(plan.storage.items()):
            prev = f"  [was: {spec.previous_backend}]" if spec.previous_backend else ""
            log.info("  ~ %-40s backend=%s%s", name, spec.backend, prev)
    else:
        log.info("Storage: (none)")

    log.info("")

    if plan.compute:
        log.info("Compute:")
        for name, cspec in sorted(plan.compute.items()):
            prev = (
                f"  [was: {cspec.previous_instance_type}]" if cspec.previous_instance_type else ""
            )
            log.info("  ~ %-40s instance=%s%s", name, cspec.instance_type, prev)
    else:
        log.info("Compute: (none)")


def _print_diff(plan_diff: "PlanDiff") -> None:
    """Print a structured diff between two plans."""
    old, new = plan_diff.old, plan_diff.new
    log.info("Diff: %s v%s → %s v%s", old.app_name, old.version, new.app_name, new.version)
    log.info("")

    for section, entries, key in (
        ("Storage", plan_diff.storage, "backend"),
        ("Compute", plan_diff.compute, "instance"),
    ):
        log.info("%s:", section)
        if not entries:
            log.info("  (no changes)")
        else:
            for entry in entries:
                if entry.change == "added":
                    log.info("  + %-40s %s=%s", entry.name, key, entry.after)
                elif entry.change == "removed":
                    log.info("  - %-40s %s=%s", entry.name, key, entry.before)
                else:  # modified
                    log.info("  ~ %-40s %s: %s → %s", entry.name, key, entry.before, entry.after)
        log.info("")


@app.callback(invoke_without_command=True)
@cli_error_boundary
def diff(
    module_app: Optional[str] = typer.Argument(
        None,
        help="Optional 'module:app' to re-solve and diff against current plan.",
        metavar="MODULE:APP",
    ),
    plan: str = typer.Option(
        _PLAN_DEFAULT,
        "--plan",
        help="Path to the plan lock file.",
    ),
) -> None:
    """
    Show what would change between the current plan and a freshly-solved plan.

    Without MODULE:APP, pretty-prints the existing plan file.
    With MODULE:APP, re-solves and diffs the result against the current plan.
    """
    from skaal import api

    if module_app is None:
        from skaal.plan import PlanFile

        _print_plan_summary(PlanFile.read(Path(plan)))
        return

    plan_diff = api.diff(old_plan=plan, app=module_app)

    _print_diff(plan_diff)
