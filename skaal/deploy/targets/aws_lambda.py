from __future__ import annotations

from pathlib import Path
from typing import Any

from skaal.deploy.packaging.lambda_zip import LambdaZipPackager
from skaal.deploy.pulumi import PulumiClient
from skaal.deploy.reporting import DeployReporter
from skaal.deploy.targets.base import Target


class AWSLambdaBuilder:
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
        del dev, stack_profile
        from skaal.deploy.aws import generate_artifacts

        return generate_artifacts(
            app=app,
            plan=plan,
            output_dir=output_dir,
            source_module=source_module,
            app_var=app_var,
            region=region or "us-east-1",
        )


class AWSLambdaDeployer:
    def __init__(self, packager: LambdaZipPackager | None = None) -> None:
        self._packager = packager or LambdaZipPackager()

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
        del gcp_project, runtime_options, app_name
        assert reporter is not None

        client = PulumiClient(artifacts_dir)
        client.select_or_init_stack(stack)
        client.config_set({"aws:region": region or "us-east-1"})
        if config_overrides:
            client.config_set(config_overrides)

        reporter.step("Packaging Lambda")
        self._packager.package(
            artifacts_dir,
            project_root=project_root,
            source_module=source_module,
        )

        reporter.step("Deploying AWS Lambda stack")
        client.up(
            yes=yes,
            stage="deploy AWS Lambda stack",
            recovery_hint=(
                "Validate AWS credentials, region configuration, and the generated Pulumi "
                "program, then rerun the deploy."
            ),
        )

        api_url = client.output("apiUrl")
        reporter.result(f"App URL: {api_url}")
        return {"apiUrl": api_url}


target = Target(
    name="aws",
    aliases=("aws-lambda",),
    builder=AWSLambdaBuilder(),
    deployer=AWSLambdaDeployer(),
    default_region="us-east-1",
)
