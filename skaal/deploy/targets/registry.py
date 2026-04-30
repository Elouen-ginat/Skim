from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypeAlias

import docker.errors as docker_errors

import skaal.deploy.targets.aws as aws_target
import skaal.deploy.targets.gcp as gcp_target
import skaal.deploy.targets.local as local_target
from skaal.deploy._progress import ProgressSink
from skaal.deploy.builders.local import local_image_name
from skaal.deploy.errors import DeployError
from skaal.deploy.packaging import build_and_push_image, package_lambda
from skaal.deploy.packaging.local import build_local_image
from skaal.deploy.pulumi import (
    DeploymentContext,
    PackageStep,
    PostUpStep,
    PulumiRunner,
    RunnerPlan,
)
from skaal.deploy.pulumi.meta import read_meta
from skaal.deploy.pulumi.runner import AutomationRunner
from skaal.deploy.target import DeployTarget
from skaal.types import AppLike, ConfigOverrides, StackOutputs, StackProfile, TargetName

if TYPE_CHECKING:
    from skaal.plan import PlanFile


deploy_log = logging.getLogger("skaal.deploy")
packaging_log = logging.getLogger("skaal.deploy.packaging")
docker_log = logging.getLogger("skaal.deploy.docker")
pulumi_log = logging.getLogger("skaal.deploy.pulumi")
_PROGRESS_SINK = ProgressSink(deploy_log)


class GenerateArtifacts(Protocol):
    def __call__(
        self,
        app: AppLike,
        plan: "PlanFile",
        output_dir: Path,
        source_module: str,
        app_var: str = "app",
        *,
        region: str | None = None,
        dev: bool = False,
        stack_profile: StackProfile | None = None,
    ) -> list[Path]: ...


BuildConfigStep: TypeAlias = Callable[[DeploymentContext, str], ConfigOverrides]


@dataclass(frozen=True)
class TargetStrategy:
    name: TargetName
    default_region: str
    generate: GenerateArtifacts
    runner: PulumiRunner
    build_config: BuildConfigStep
    package: PackageStep | None = None
    post_up: PostUpStep | None = None
    output_keys: tuple[str, ...] = ()


def _aws_config(context: DeploymentContext, default_region: str) -> ConfigOverrides:
    return {"aws:region": context.region or default_region}


def _gcp_config(context: DeploymentContext, default_region: str) -> ConfigOverrides:
    if not context.gcp_project:
        raise ValueError(
            "GCP project is required for --target=gcp. Pass --gcp-project PROJECT or set SKAAL_GCP_PROJECT."
        )
    return {
        "gcp:project": context.gcp_project,
        "gcp:region": context.region or default_region,
    }


def _local_config(context: DeploymentContext, default_region: str) -> ConfigOverrides:
    del context, default_region
    return {}


def _aws_package(context: DeploymentContext) -> ConfigOverrides:
    meta = read_meta(context.artifacts_dir)
    packaging_log.info("Packaging Lambda ...")
    package_lambda(
        context.artifacts_dir,
        context.project_root,
        context.source_module,
        lambda_architecture=meta.get("lambda_architecture", "x86_64"),
        lambda_runtime=meta.get("lambda_runtime", "python3.11"),
    )
    return {}


def _local_package(context: DeploymentContext) -> ConfigOverrides:
    docker_log.info("Building local app image ...")
    image_name = local_image_name(context.app_name)
    try:
        image_id = build_local_image(
            context.artifacts_dir,
            image_name,
            progress=_PROGRESS_SINK.docker_log,
        )
    except DeployError:
        raise
    except docker_errors.DockerException as exc:
        raise DeployError(
            target="local",
            phase="image",
            message="Failed to build the local Docker image.",
            diagnostics=str(exc),
        ) from exc
    return {"localImageRef": image_id or image_name}


def _gcp_post_up(context: DeploymentContext, output: "Callable[[str], str]") -> bool:
    if not context.gcp_project:
        raise ValueError(
            "GCP project is required for --target=gcp. Pass --gcp-project PROJECT or set SKAAL_GCP_PROJECT."
        )
    repository = output("imageRepository")
    resolved_region = context.region or "us-central1"
    docker_log.info("Building and pushing image to %s ...", repository)
    try:
        build_and_push_image(
            context.artifacts_dir,
            context.gcp_project,
            resolved_region,
            repository,
            context.app_name,
            progress=_PROGRESS_SINK.docker_log,
        )
    except DeployError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DeployError(
            target="gcp",
            phase="image",
            message="Failed to build or push the Cloud Run image.",
            diagnostics=str(exc),
        ) from exc
    pulumi_log.info("Deploying image to Cloud Run (pulumi up) ...")
    return True


_runner = AutomationRunner(progress_sink=_PROGRESS_SINK)

_AWS_STRATEGY = TargetStrategy(
    name="aws",
    default_region="us-east-1",
    generate=aws_target.generate_artifacts,
    runner=_runner,
    build_config=_aws_config,
    package=_aws_package,
    output_keys=("apiUrl",),
)
_GCP_STRATEGY = TargetStrategy(
    name="gcp",
    default_region="us-central1",
    generate=gcp_target.generate_artifacts,
    runner=_runner,
    build_config=_gcp_config,
    post_up=_gcp_post_up,
    output_keys=("serviceUrl", "imageRepository"),
)
_LOCAL_STRATEGY = TargetStrategy(
    name="local",
    default_region="",
    generate=local_target.generate_artifacts,
    runner=_runner,
    build_config=_local_config,
    package=_local_package,
    output_keys=("appUrl",),
)


class PulumiDeployTarget(DeployTarget):
    name: str
    default_region: str

    def __init__(self, strategy: TargetStrategy):
        self._strategy = strategy
        self.name = strategy.name
        self.default_region = strategy.default_region

    def generate_artifacts(
        self,
        app: AppLike,
        plan: "PlanFile",
        output_dir: Path,
        source_module: str,
        app_var: str = "app",
        *,
        region: str | None = None,
        dev: bool = False,
        stack_profile: StackProfile | None = None,
    ) -> list[Path]:
        return self._strategy.generate(
            app=app,
            plan=plan,
            output_dir=output_dir,
            source_module=source_module,
            app_var=app_var,
            region=region or self.default_region,
            dev=dev,
            stack_profile=stack_profile,
        )

    def package_and_push(
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
        config_overrides: ConfigOverrides | None = None,
    ) -> StackOutputs:
        context = DeploymentContext(
            target=self._strategy.name,
            artifacts_dir=artifacts_dir,
            stack=stack,
            region=region,
            gcp_project=gcp_project,
            yes=yes,
            project_root=project_root,
            source_module=source_module,
            app_name=app_name,
            config_overrides=config_overrides,
        )
        config = dict(self._strategy.build_config(context, self.default_region))
        if config_overrides:
            config.update(config_overrides)
        outputs = self._strategy.runner.deploy(
            RunnerPlan(
                context=context,
                config=config,
                package=self._strategy.package,
                post_up=self._strategy.post_up,
                output_keys=self._strategy.output_keys,
            )
        )
        for key in ("apiUrl", "serviceUrl", "appUrl"):
            if key in outputs:
                deploy_log.info(
                    "App URL: %s",
                    outputs[key],
                    extra={
                        "app": context.app_name,
                        "stack": context.stack,
                        "target": context.target,
                    },
                )
                break
        return outputs

    def destroy_stack(self, artifacts_dir: Path, *, stack: str, yes: bool) -> None:
        self._strategy.runner.destroy(artifacts_dir, stack=stack, yes=yes)


_aws = PulumiDeployTarget(_AWS_STRATEGY)
_gcp = PulumiDeployTarget(_GCP_STRATEGY)
_local = PulumiDeployTarget(_LOCAL_STRATEGY)

_TARGET_REGISTRY: dict[str, DeployTarget] = {
    "aws": _aws,
    "aws-lambda": _aws,
    "gcp": _gcp,
    "gcp-cloudrun": _gcp,
    "local": _local,
    "local-docker": _local,
}


def get_target(name: str) -> DeployTarget:
    try:
        return _TARGET_REGISTRY[name]
    except KeyError:
        known = sorted({target.name for target in _TARGET_REGISTRY.values()})
        raise ValueError(f"Unknown deploy target {name!r}. Supported targets: {known}") from None
