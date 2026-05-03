"""Skaal deploy facade."""

from __future__ import annotations

from pathlib import Path

from skaal.deploy.pulumi.meta import read_meta
from skaal.types import ConfigOverrides, StackOutputs

from .targets.registry import get_target


def package_and_push(
    artifacts_dir: Path,
    *,
    stack: str = "dev",
    region: str | None = None,
    gcp_project: str | None = None,
    yes: bool = True,
    config_overrides: ConfigOverrides | None = None,
) -> StackOutputs:
    artifacts_dir = Path(artifacts_dir).resolve()
    meta = read_meta(artifacts_dir)
    return get_target(meta["target"]).package_and_push(
        artifacts_dir,
        stack=stack,
        region=region,
        gcp_project=gcp_project,
        yes=yes,
        project_root=artifacts_dir.parent,
        source_module=meta["source_module"],
        app_name=meta["app_name"],
        config_overrides=config_overrides,
    )


def destroy_stack(
    artifacts_dir: Path,
    *,
    stack: str = "dev",
    yes: bool = True,
) -> None:
    artifacts_dir = Path(artifacts_dir).resolve()
    meta = read_meta(artifacts_dir)
    get_target(meta["target"]).destroy_stack(artifacts_dir, stack=stack, yes=yes)


__all__ = ["destroy_stack", "get_target", "package_and_push"]
