"""`skaal diff` — show infrastructure changes between plan versions."""

from __future__ import annotations

from typing import Optional

import typer

app = typer.Typer(help="Show what changes between plan versions.")


@app.callback(invoke_without_command=True)
def diff(
    version_range: Optional[str] = typer.Argument(None, help="Version range, e.g. v1..v2."),
) -> None:
    """
    Show schema changes, backend migrations, compute changes, and risk assessment
    between the current plan and the new plan (or between two explicit versions).
    """
    raise NotImplementedError("`skaal diff` is not yet implemented (Phase 5).")
