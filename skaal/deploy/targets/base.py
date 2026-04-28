from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from skaal.deploy.reporting import DeployReporter, SilentReporter

if TYPE_CHECKING:
    from skaal.plan import PlanFile


@dataclass(frozen=True, slots=True)
class BuildOptions:
    region: str | None = None
    dev: bool = False
    stack_profile: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class DeployOptions:
    stack: str
    project_root: Path
    source_module: str
    app_name: str
    region: str | None = None
    gcp_project: str | None = None
    yes: bool = True
    config_overrides: dict[str, str] | None = None
    runtime_options: dict[str, Any] | None = None


class ArtifactBuilder(Protocol):
    def build(
        self,
        app: Any,
        plan: "PlanFile",
        output_dir: Path,
        source_module: str,
        app_var: str = "app",
        options: BuildOptions | None = None,
    ) -> list[Path]: ...


class ArtifactDeployer(Protocol):
    def deploy(
        self,
        artifacts_dir: Path,
        options: DeployOptions,
        reporter: DeployReporter | None = None,
    ) -> dict[str, str]: ...


@dataclass(frozen=True)
class Target:
    name: str
    aliases: tuple[str, ...]
    builder: ArtifactBuilder
    deployer: ArtifactDeployer
    default_region: str | None = None

    @staticmethod
    def _coerce_build_options(
        options: BuildOptions | None,
        legacy_kwargs: dict[str, Any],
    ) -> BuildOptions:
        if options is not None:
            if legacy_kwargs:
                raise TypeError(
                    "Target.build() accepts either options=... or legacy kwargs, not both."
                )
            return options

        resolved_options = BuildOptions(
            region=legacy_kwargs.pop("region", None),
            dev=legacy_kwargs.pop("dev", False),
            stack_profile=legacy_kwargs.pop("stack_profile", None),
        )
        if legacy_kwargs:
            unexpected = ", ".join(sorted(legacy_kwargs))
            raise TypeError(f"Target.build() got unexpected keyword argument(s): {unexpected}")
        return resolved_options

    @staticmethod
    def _coerce_deploy_options(
        options: DeployOptions | None,
        legacy_kwargs: dict[str, Any],
    ) -> DeployOptions:
        if options is not None:
            if legacy_kwargs:
                raise TypeError(
                    "Target.deploy() accepts either options=... or legacy kwargs, not both."
                )
            return options

        required = ("stack", "project_root", "source_module", "app_name")
        missing = [name for name in required if name not in legacy_kwargs]
        if missing:
            missing_names = ", ".join(missing)
            raise TypeError(
                f"Target.deploy() missing required keyword argument(s): {missing_names}"
            )

        resolved_options = DeployOptions(
            stack=legacy_kwargs.pop("stack"),
            project_root=legacy_kwargs.pop("project_root"),
            source_module=legacy_kwargs.pop("source_module"),
            app_name=legacy_kwargs.pop("app_name"),
            region=legacy_kwargs.pop("region", None),
            gcp_project=legacy_kwargs.pop("gcp_project", None),
            yes=legacy_kwargs.pop("yes", True),
            config_overrides=legacy_kwargs.pop("config_overrides", None),
            runtime_options=legacy_kwargs.pop("runtime_options", None),
        )
        if legacy_kwargs:
            unexpected = ", ".join(sorted(legacy_kwargs))
            raise TypeError(f"Target.deploy() got unexpected keyword argument(s): {unexpected}")
        return resolved_options

    def build(
        self,
        app: Any,
        plan: "PlanFile",
        output_dir: Path,
        source_module: str,
        app_var: str = "app",
        options: BuildOptions | None = None,
        **legacy_kwargs: Any,
    ) -> list[Path]:
        resolved_options = self._coerce_build_options(options, legacy_kwargs)
        if resolved_options.region is None and self.default_region is not None:
            resolved_options = replace(resolved_options, region=self.default_region)
        return self.builder.build(
            app=app,
            plan=plan,
            output_dir=output_dir,
            source_module=source_module,
            app_var=app_var,
            options=resolved_options,
        )

    def deploy(
        self,
        artifacts_dir: Path,
        options: DeployOptions | None = None,
        reporter: DeployReporter | None = None,
        **legacy_kwargs: Any,
    ) -> dict[str, str]:
        resolved_options = self._coerce_deploy_options(options, legacy_kwargs)
        if resolved_options.region is None and self.default_region is not None:
            resolved_options = replace(resolved_options, region=self.default_region)
        return self.deployer.deploy(
            artifacts_dir,
            options=resolved_options,
            reporter=reporter or SilentReporter(),
        )
