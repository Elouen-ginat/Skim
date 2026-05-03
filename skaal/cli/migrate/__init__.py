"""`skaal migrate` — data and relational migration sub-apps."""

from __future__ import annotations

import typer

from skaal.cli.migrate.data_cmd import app as data_app
from skaal.cli.migrate.relational_cmd import app as relational_app

app = typer.Typer(
    help="Manage data and relational migrations.",
    no_args_is_help=True,
)
app.add_typer(data_app, name="data")
app.add_typer(relational_app, name="relational")

__all__ = ["app"]
