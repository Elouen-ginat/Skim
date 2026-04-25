from __future__ import annotations

from pathlib import Path
from typing import Any

from skaal.deploy.push import read_meta
from skaal.deploy.registry import get_target
from skaal.deploy.reporting import DeployReporter, SilentReporter


def build_artifacts(
    *,
    app: Any,
    plan: Any,
    output_dir: Path,
    source_module: str,
    app_var: str = "app",
    region: str | None = None,
    dev: bool = False,
    stack_profile: dict[str, Any] | None = None,
) -> list[Path]:
    return get_target(plan.deploy_target).build(
        app=app,
        plan=plan,
        output_dir=output_dir,
        source_module=source_module,
        app_var=app_var,
        region=region,
        dev=dev,
        stack_profile=stack_profile,
    )


def deploy_artifacts(
    artifacts_dir: Path,
    *,
    stack: str = "dev",
    region: str | None = None,
    gcp_project: str | None = None,
    yes: bool = True,
    config_overrides: dict[str, str] | None = None,
    runtime_options: dict[str, Any] | None = None,
    reporter: DeployReporter | None = None,
) -> dict[str, str]:
    resolved_dir = Path(artifacts_dir).resolve()
    meta = read_meta(resolved_dir)
    target = get_target(meta["target"])
    return target.deploy(
        resolved_dir,
        stack=stack,
        region=region,
        gcp_project=gcp_project,
        yes=yes,
        project_root=resolved_dir.parent,
        source_module=meta["source_module"],
        app_name=meta["app_name"],
        config_overrides=config_overrides,
        runtime_options=runtime_options,
        reporter=reporter or SilentReporter(),
    )


__all__ = ["build_artifacts", "deploy_artifacts"]
