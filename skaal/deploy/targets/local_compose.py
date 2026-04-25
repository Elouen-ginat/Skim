from __future__ import annotations

from pathlib import Path
from typing import Any

from skaal.deploy.pulumi import run_command
from skaal.deploy.reporting import DeployReporter
from skaal.deploy.targets.base import Target


class LocalComposeBuilder:
    def build(
        self,
        app: Any,
        plan: Any,
        output_dir: Path,
        source_module: str,
        app_var: str = "app",
        *,
        region: str | None = None,
        dev: bool = False,
        stack_profile: dict[str, Any] | None = None,
    ) -> list[Path]:
        del region, stack_profile
        from skaal.deploy.local import generate_artifacts

        return generate_artifacts(
            app=app,
            plan=plan,
            output_dir=output_dir,
            source_module=source_module,
            app_var=app_var,
            dev=dev,
        )


class LocalComposeDeployer:
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
        del stack, region, gcp_project, yes, project_root, source_module, app_name, config_overrides
        assert reporter is not None

        detach = bool((runtime_options or {}).get("detach", False))
        follow_logs = bool((runtime_options or {}).get("follow_logs", False))

        cmd = ["docker", "compose", "up", "--build"]
        if detach:
            cmd.append("--detach")

        reporter.step("Starting local Docker Compose stack")
        run_command(
            cmd,
            cwd=artifacts_dir,
            stage="start local Docker Compose stack",
            recovery_hint=(
                "Confirm Docker Desktop is running and that the generated artifacts include a "
                "valid docker-compose.yml file."
            ),
        )

        if detach and follow_logs:
            reporter.step("Following local Docker Compose logs")
            run_command(
                ["docker", "compose", "logs", "--follow"],
                cwd=artifacts_dir,
                stage="follow local Docker Compose logs",
                recovery_hint=(
                    "Confirm the local compose stack is running, or rerun `docker compose up` "
                    "manually from the artifacts directory."
                ),
            )

        return {}


target = Target(
    name="local",
    aliases=("local-compose",),
    builder=LocalComposeBuilder(),
    deployer=LocalComposeDeployer(),
)
