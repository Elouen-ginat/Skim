from __future__ import annotations

from pathlib import Path

from skaal.deploy.pulumi import RunnerPlan
from skaal.deploy.targets.registry import PulumiDeployTarget, TargetStrategy


class _FakeRunner:
    def __init__(self) -> None:
        self.plan: RunnerPlan | None = None
        self.destroy_args: tuple[Path, str, bool] | None = None

    def deploy(self, plan: RunnerPlan) -> dict[str, str]:
        self.plan = plan
        return {"appUrl": "http://localhost:8000"}

    def destroy(self, artifacts_dir: Path, *, stack: str, yes: bool) -> None:
        self.destroy_args = (artifacts_dir, stack, yes)


def test_pulumi_target_forwards_context_and_config() -> None:
    runner = _FakeRunner()
    target = PulumiDeployTarget(
        TargetStrategy(
            name="local",
            default_region="",
            generate=lambda **kwargs: [],
            runner=runner,
            build_config=lambda context, default_region: {"baseConfig": "base"},
            package=lambda context: {"localImageRef": "sha256:local-image"},
            output_keys=("appUrl",),
        )
    )

    outputs = target.package_and_push(
        Path("artifacts"),
        stack="local",
        region=None,
        gcp_project=None,
        yes=True,
        project_root=Path("."),
        source_module="examples.counter",
        app_name="Test App",
        config_overrides={"extraConfig": "extra"},
    )

    assert outputs == {"appUrl": "http://localhost:8000"}
    assert runner.plan is not None
    assert runner.plan.context.target == "local"
    assert runner.plan.config == {
        "baseConfig": "base",
        "extraConfig": "extra",
    }
    assert runner.plan.package is not None


def test_pulumi_target_destroy_delegates_to_runner() -> None:
    runner = _FakeRunner()
    target = PulumiDeployTarget(
        TargetStrategy(
            name="local",
            default_region="",
            generate=lambda **kwargs: [],
            runner=runner,
            build_config=lambda context, default_region: {},
        )
    )

    target.destroy_stack(Path("artifacts"), stack="local", yes=True)

    assert runner.destroy_args == (Path("artifacts"), "local", True)
