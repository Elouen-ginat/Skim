"""``skaal stacks`` — list configured stack profiles.

Reads ``[tool.skaal.stacks.<name>]`` sections from the nearest
``pyproject.toml`` and prints one row per profile with the resolved region,
GCP project, and deploy target (after applying each profile over the base
settings).  Useful sanity-check before running ``skaal deploy --stack X``.
"""

from __future__ import annotations

import logging

import typer

from skaal.cli._errors import cli_error_boundary
from skaal.settings import SkaalSettings

app = typer.Typer(help="List configured stack profiles.")
log = logging.getLogger("skaal.cli")


@app.callback(invoke_without_command=True)
@cli_error_boundary
def stacks() -> None:
    """Print one row per declared stack profile."""
    base = SkaalSettings()
    if not base.stacks:
        log.info(
            "No stacks configured. Add profiles under [tool.skaal.stacks.<name>] "
            "in pyproject.toml."
        )
        return

    log.info(f"{'stack':<18} {'target':<10} {'region':<15} {'gcp_project':<24} {'protect':<8} hooks")
    log.info(f"{'-' * 18} {'-' * 10} {'-' * 15} {'-' * 24} {'-' * 8} {'-' * 5}")

    for name in sorted(base.stacks):
        cfg = base.for_stack(name)
        marker = "*" if name == base.stack else ""
        protect = (
            "yes"
            if cfg.deletion_protection is True
            else "no"
            if cfg.deletion_protection is False
            else "-"
        )
        hook_count = len(cfg.pre_deploy) + len(cfg.post_deploy)
        hooks = str(hook_count) if hook_count else "-"
        log.info(
            f"{marker}{name:<17} {cfg.target:<10} {cfg.region:<15} "
            f"{(cfg.gcp_project or '-'):<24} {protect:<8} {hooks}"
        )

    if base.stack in base.stacks:
        log.info("* = current default stack (tool.skaal.stack)")
