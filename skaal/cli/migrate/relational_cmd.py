"""`skaal migrate relational` — Alembic-driven schema migrations."""

from __future__ import annotations

import asyncio
import logging

import typer

from skaal.cli._errors import cli_error_boundary
from skaal.cli._utils import resolve_app_ref

app = typer.Typer(help="Alembic-driven SQLModel schema migrations.")
log = logging.getLogger("skaal.cli")


def _backend_opt() -> typer.Option:
    return typer.Option(
        None,
        "--backend",
        help="Restrict to one resolved relational backend (e.g. sqlite, postgres).",
    )


@app.command("autogenerate")
@cli_error_boundary
def autogenerate(
    message: str = typer.Option(..., "--message", "-m", help="Revision message."),
    backend: str | None = _backend_opt(),
) -> None:
    """Generate a new revision by diffing models against the live database."""
    from skaal import api

    app_obj = resolve_app_ref()
    results = asyncio.run(
        api.relational_autogenerate(app_obj, message=message, backend_name=backend)
    )
    for backend_label, revision in results.items():
        if revision is None:
            log.info("[%s] no changes detected.", backend_label)
        else:
            log.info(
                "[%s] created revision %s — %s",
                backend_label,
                revision.revision_id,
                revision.message,
            )


@app.command("upgrade")
@cli_error_boundary
def upgrade(
    target: str = typer.Argument("head", help="Target revision (default: head)."),
    backend: str | None = _backend_opt(),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the SQL without applying."),
) -> None:
    """Apply pending migrations up to *target*."""
    from skaal import api

    app_obj = resolve_app_ref()
    if dry_run:
        plans = asyncio.run(
            api.relational_plan_upgrade(app_obj, target=target, backend_name=backend)
        )
        _print_plans(plans)
        return
    statuses = asyncio.run(api.relational_upgrade(app_obj, target=target, backend_name=backend))
    _print_statuses(statuses)


@app.command("downgrade")
@cli_error_boundary
def downgrade(
    target: str = typer.Argument(..., help="Target revision (e.g. -1, base, <revision_id>)."),
    backend: str | None = _backend_opt(),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the SQL without applying."),
) -> None:
    """Roll back to *target*."""
    from skaal import api

    app_obj = resolve_app_ref()
    if dry_run:
        plans = asyncio.run(
            api.relational_plan_downgrade(app_obj, target=target, backend_name=backend)
        )
        _print_plans(plans)
        return
    statuses = asyncio.run(api.relational_downgrade(app_obj, target=target, backend_name=backend))
    _print_statuses(statuses)


@app.command("current")
@cli_error_boundary
def current(backend: str | None = _backend_opt()) -> None:
    """Show the currently applied revision per backend."""
    from skaal import api

    app_obj = resolve_app_ref()
    statuses = asyncio.run(api.relational_current(app_obj, backend_name=backend))
    _print_statuses(statuses)


@app.command("history")
@cli_error_boundary
def history(backend: str | None = _backend_opt()) -> None:
    """List every revision present in versions/, newest first."""
    from skaal import api

    app_obj = resolve_app_ref()
    histories = asyncio.run(api.relational_history(app_obj, backend_name=backend))
    for backend_label, revisions in histories.items():
        log.info("[%s]", backend_label)
        if not revisions:
            log.info("  (no revisions)")
            continue
        for rev in revisions:
            marker = "*" if rev.is_applied else " "
            head = " (head)" if rev.is_head else ""
            log.info("  %s %s — %s%s", marker, rev.revision_id, rev.message, head)


@app.command("check")
@cli_error_boundary
def check(backend: str | None = _backend_opt()) -> None:
    """Exit non-zero if the live schema has drifted from the registered models."""
    from skaal import api

    app_obj = resolve_app_ref()
    plans = asyncio.run(api.relational_check(app_obj, backend_name=backend))
    drifted = False
    for backend_label, plan in plans.items():
        if plan.is_empty:
            log.info("[%s] no drift.", backend_label)
            continue
        drifted = True
        log.error("[%s] %d pending change(s):", backend_label, len(plan.steps))
        for step in plan.steps:
            log.error("  %s %s — %s", step.op.value, step.table or "-", step.detail)
    if drifted:
        raise typer.Exit(code=1)


@app.command("stamp")
@cli_error_boundary
def stamp(
    target: str = typer.Argument(..., help="Revision id to stamp the database at."),
    backend: str | None = _backend_opt(),
) -> None:
    """Mark the database as being at *target* without running any migrations."""
    from skaal import api

    app_obj = resolve_app_ref()
    statuses = asyncio.run(api.relational_stamp(app_obj, target=target, backend_name=backend))
    _print_statuses(statuses)


def _print_statuses(statuses: dict) -> None:
    for label, status in statuses.items():
        head = status.head_revision or "(none)"
        current_rev = status.current_revision or "(none)"
        log.info(
            "[%s] current=%s head=%s pending=%d", label, current_rev, head, len(status.pending)
        )


def _print_plans(plans: dict) -> None:
    for label, plan in plans.items():
        log.info(
            "[%s] %s %s -> %s (%d steps)",
            label,
            plan.direction,
            plan.from_revision or "base",
            plan.to_revision,
            len(plan.steps),
        )
        for step in plan.steps:
            log.info("  %s", step.sql.strip() or step.detail)
