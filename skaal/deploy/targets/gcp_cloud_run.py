from __future__ import annotations

from pathlib import Path
from typing import Any

from skaal.deploy.packaging.container_image import ContainerImagePackager
from skaal.deploy.pulumi import DeployCommandError, PulumiClient
from skaal.deploy.reporting import DeployReporter
from skaal.deploy.targets.base import Target


class GCPCloudRunBuilder:
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
        del dev
        from skaal.deploy.gcp import generate_artifacts

        return generate_artifacts(
            app=app,
            plan=plan,
            output_dir=output_dir,
            source_module=source_module,
            app_var=app_var,
            region=region or "us-central1",
            stack_profile=stack_profile,
        )


class GCPCloudRunDeployer:
    def __init__(self, image_packager: ContainerImagePackager | None = None) -> None:
        self._image_packager = image_packager or ContainerImagePackager()

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
        del project_root, source_module, runtime_options
        assert reporter is not None

        if not gcp_project:
            raise ValueError(
                "GCP project is required for the gcp target. "
                "Pass --gcp-project PROJECT or set SKAAL_GCP_PROJECT."
            )

        resolved_region = region or "us-central1"
        client = PulumiClient(artifacts_dir)
        client.select_or_init_stack(stack)
        client.config_set({"gcp:project": gcp_project, "gcp:region": resolved_region})
        if config_overrides:
            client.config_set(config_overrides)

        reporter.step("Provisioning GCP infrastructure")
        client.up(
            yes=yes,
            stage="provision GCP infrastructure",
            recovery_hint=(
                "Validate GCP credentials, region, and service APIs, then rerun the deploy."
            ),
        )

        repo = client.output("imageRepository")
        reporter.step(f"Building and pushing image to {repo}")
        try:
            self._image_packager.publish(
                artifacts_dir,
                project=gcp_project,
                region=resolved_region,
                repository=repo,
                image_name=app_name,
            )
        except DeployCommandError as exc:
            raise exc.with_recovery_hint(
                "The infrastructure stack was already created. Fix Docker or gcloud authentication "
                "and rerun the deploy, or roll the stack back manually if needed."
            ) from exc

        reporter.step("Deploying Cloud Run service revision")
        try:
            client.up(
                yes=yes,
                stage="deploy Cloud Run service revision",
                recovery_hint=(
                    "The container image may already be published. Re-run the deploy to retry the "
                    "service update, or inspect the stack with `pulumi preview`."
                ),
            )
        except DeployCommandError as exc:
            raise exc.with_recovery_hint(
                "The container image may already be published. Re-run the deploy after correcting "
                "the Pulumi or GCP configuration."
            ) from exc

        service_url = client.output("serviceUrl")
        reporter.result(f"App URL: {service_url}")
        return {"serviceUrl": service_url}


target = Target(
    name="gcp",
    aliases=("gcp-cloudrun",),
    builder=GCPCloudRunBuilder(),
    deployer=GCPCloudRunDeployer(),
    default_region="us-central1",
)
