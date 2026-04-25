from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from skaal.deploy.reporting import DeployReporter, SilentReporter

if TYPE_CHECKING:
    from skaal.plan import PlanFile


class ArtifactBuilder(Protocol):
    def build(
        self,
        app: Any,
        plan: "PlanFile",
        output_dir: Path,
        source_module: str,
        app_var: str = "app",
        *,
        region: str | None = None,
        dev: bool = False,
        stack_profile: dict[str, Any] | None = None,
    ) -> list[Path]: ...


class ArtifactDeployer(Protocol):
    def deploy(
        self,
        artifacts_dir: Path,
        *,
        stack: str,
        region: str | None,
        gcp_project: str | None,
        yes: bool,
        project_root: Path,
        source_module: str,
        app_name: str,
        config_overrides: dict[str, str] | None = None,
        runtime_options: dict[str, Any] | None = None,
        reporter: DeployReporter | None = None,
    ) -> dict[str, str]: ...


@dataclass(frozen=True)
class Target:
    name: str
    aliases: tuple[str, ...]
    builder: ArtifactBuilder
    deployer: ArtifactDeployer
    default_region: str | None = None

    def build(
        self,
        app: Any,
        plan: "PlanFile",
        output_dir: Path,
        source_module: str,
        app_var: str = "app",
        *,
        region: str | None = None,
        dev: bool = False,
        stack_profile: dict[str, Any] | None = None,
    ) -> list[Path]:
        return self.builder.build(
            app=app,
            plan=plan,
            output_dir=output_dir,
            source_module=source_module,
            app_var=app_var,
            region=region or self.default_region,
            dev=dev,
            stack_profile=stack_profile,
        )

    def deploy(
        self,
        artifacts_dir: Path,
        *,
        stack: str,
        region: str | None,
        gcp_project: str | None,
        yes: bool,
        project_root: Path,
        source_module: str,
        app_name: str,
        config_overrides: dict[str, str] | None = None,
        runtime_options: dict[str, Any] | None = None,
        reporter: DeployReporter | None = None,
    ) -> dict[str, str]:
        return self.deployer.deploy(
            artifacts_dir,
            stack=stack,
            region=region or self.default_region,
            gcp_project=gcp_project,
            yes=yes,
            project_root=project_root,
            source_module=source_module,
            app_name=app_name,
            config_overrides=config_overrides,
            runtime_options=runtime_options,
            reporter=reporter or SilentReporter(),
        )
