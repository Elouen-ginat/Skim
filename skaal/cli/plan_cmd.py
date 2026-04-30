"""`skaal plan` — run constraint solver, generate plan.skaal.lock."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import typer

from skaal.cli._errors import cli_error_boundary
from skaal.cli._utils import load_app  # noqa: F401 — re-exported for test patching
from skaal.cli.config import SkaalSettings

if TYPE_CHECKING:
    from skaal.plan import PlanFile

app = typer.Typer(help="Run the constraint solver and generate a plan.")
log = logging.getLogger("skaal.cli")


def _print_plan_table(plan_file: "PlanFile") -> None:
    """Pretty-print a plan's storage assignments as a bordered table."""
    col_w = [30, 25, 60]
    header = (
        f"| {'Storage variable':<{col_w[0]}} | {'Backend':<{col_w[1]}} | "
        f"{'Reason':<{col_w[2]}} |"
    )
    sep = f"+-{'-' * col_w[0]}-+-{'-' * col_w[1]}-+-{'-' * col_w[2]}-+"
    log.info(sep)
    log.info(header)
    log.info(sep)
    for spec in plan_file.storage.values():
        log.info(
            f"| {spec.variable_name:<{col_w[0]}} | "
            f"{spec.backend:<{col_w[1]}} | "
            f"{spec.reason[: col_w[2]]:<{col_w[2]}} |"
        )
    log.info(sep)

    if plan_file.compute:
        log.info("")
        log.info("Compute assignments:")
        for cspec in plan_file.compute.values():
            log.info("  %s: %s  (%s)", cspec.function_name, cspec.instance_type, cspec.reason)


@app.callback(invoke_without_command=True)
@cli_error_boundary
def plan(
    target_app: Optional[str] = typer.Argument(
        None,
        help=(
            "App to plan as 'module:variable', e.g. 'examples.counter:app'. "
            "Falls back to 'app' in [tool.skaal] of pyproject.toml."
        ),
        metavar="MODULE:APP",
    ),
    target: Optional[str] = typer.Option(
        None,
        "--target",
        "-t",
        help=(
            "Deploy target: aws, gcp, aws-lambda, gcp-cloudrun, k8s, ecs. "
            "Env: SKAAL_TARGET. pyproject: tool.skaal.target."
        ),
    ),
    catalog: Optional[Path] = typer.Option(
        None,
        "--catalog",
        help="Path to catalog TOML. Env: SKAAL_CATALOG. pyproject: tool.skaal.catalog.",
    ),
    reoptimize: bool = typer.Option(
        False, "--reoptimize", help="Force re-solving all backend choices."
    ),
    pin: list[str] = typer.Option(
        [], "--pin", help="Pin a variable to a backend, e.g. profiles=redis."
    ),
) -> None:
    """
    Analyze the app's constraints via Z3 and write plan.skaal.lock.

    Solver output includes: backend selections, instance types, placement rules,
    estimated cost, and UNSAT explanations if constraints cannot be met.

    Example:

        skaal plan examples.counter:app --target aws
    """
    from skaal import api
    from skaal.plan import PLAN_FILE_NAME
    from skaal.solver.storage import UnsatisfiableConstraints

    cfg = SkaalSettings()
    resolved_app = target_app or cfg.app
    resolved_target = target or cfg.target

    if resolved_app is None:
        raise ValueError(
            "missing MODULE:APP.\n"
            "  Pass it as an argument: skaal plan mypackage.app:skaal_app\n"
            "  Or set 'app' in [tool.skaal] of pyproject.toml."
        )

    skaal_app = api.resolve_app(resolved_app)

    log.info("Solving constraints for %r -> target=%r ...", skaal_app.name, resolved_target)

    try:
        plan_file = api.plan(
            resolved_app,
            target=resolved_target,
            catalog=catalog,
            write=True,
        )
    except UnsatisfiableConstraints as exc:
        raise ValueError(str(exc)) from exc

    log.info("Wrote %s", PLAN_FILE_NAME)
    log.info("")
    _print_plan_table(plan_file)
