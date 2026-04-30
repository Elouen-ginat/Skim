"""Entry point for the `skaal` CLI."""

from typing import Optional

import typer

from skaal.cli._logging import LogFormat, configure_cli_logging
from skaal.cli.build_cmd import app as build_app
from skaal.cli.catalog_cmd import app as catalog_app
from skaal.cli.deploy_cmd import app as deploy_app
from skaal.cli.destroy_cmd import app as destroy_app
from skaal.cli.diff_cmd import app as diff_app
from skaal.cli.infra_cmd import app as infra_app
from skaal.cli.migrate_cmd import app as migrate_app
from skaal.cli.plan_cmd import app as plan_app
from skaal.cli.run_cmd import app as run_app
from skaal.cli.stacks_cmd import app as stacks_app

app = typer.Typer(
    name="skaal",
    help="Skaal — Infrastructure as Constraints. Write it once. Scale it with a word.",
    no_args_is_help=True,
)

app.add_typer(run_app, name="run")
app.add_typer(plan_app, name="plan")
app.add_typer(build_app, name="build")
app.add_typer(deploy_app, name="deploy")
app.add_typer(destroy_app, name="destroy")
app.add_typer(catalog_app, name="catalog")
app.add_typer(diff_app, name="diff")
app.add_typer(infra_app, name="infra")
app.add_typer(migrate_app, name="migrate")
app.add_typer(stacks_app, name="stacks")


@app.callback()
def _root(
    verbose: int = typer.Option(
        0,
        "--verbose",
        "-v",
        count=True,
        help="Increase log verbosity. -v=INFO, -vv=DEBUG.",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Suppress INFO logs. Errors still print.",
    ),
    log_format: Optional[LogFormat] = typer.Option(
        None,
        "--log-format",
        help="text or json. Env: SKAAL_LOG_FORMAT.",
        case_sensitive=False,
    ),
) -> None:
    configure_cli_logging(verbose=verbose, quiet=quiet, fmt=log_format)


if __name__ == "__main__":
    app()
