from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeAlias

from skaal.types import ConfigOverrides, StackOutputs, TargetName


@dataclass(frozen=True)
class DeploymentContext:
    target: TargetName
    artifacts_dir: Path
    stack: str
    region: str | None
    gcp_project: str | None
    yes: bool
    project_root: Path
    source_module: str
    app_name: str
    config_overrides: ConfigOverrides | None = None


PackageStep: TypeAlias = Callable[[DeploymentContext], ConfigOverrides | None]
PostUpStep: TypeAlias = Callable[[DeploymentContext, Callable[[str], str]], bool]


@dataclass(frozen=True)
class RunnerPlan:
    context: DeploymentContext
    config: ConfigOverrides
    output_keys: tuple[str, ...]
    package: PackageStep | None = None
    post_up: PostUpStep | None = None


class PulumiRunner(Protocol):
    def deploy(self, plan: RunnerPlan) -> StackOutputs: ...

    def destroy(self, artifacts_dir: Path, *, stack: str, yes: bool) -> None: ...
