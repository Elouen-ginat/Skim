"""Entry point for the `skim` CLI."""

import typer

from skaal.cli.run_cmd import app as run_app
from skaal.cli.plan_cmd import app as plan_app
from skaal.cli.deploy_cmd import app as deploy_app
from skaal.cli.migrate_cmd import app as migrate_app
from skaal.cli.diff_cmd import app as diff_app
from skaal.cli.infra_cmd import app as infra_app

app = typer.Typer(
    name="skaal",
    help="Skaal — Infrastructure as Constraints. Write it once. Scale it with a word.",
    no_args_is_help=True,
)

app.add_typer(run_app, name="run")
app.add_typer(plan_app, name="plan")
app.add_typer(deploy_app, name="deploy")
app.add_typer(migrate_app, name="migrate")
app.add_typer(diff_app, name="diff")
app.add_typer(infra_app, name="infra")


if __name__ == "__main__":
    app()
