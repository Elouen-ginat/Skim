"""GCP Cloud Run deploy target — artifact generation, packaging, and deploy."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from skaal.deploy.pulumi import DeployCommandError, PulumiClient
from skaal.deploy.push import build_push_image, write_meta
from skaal.deploy.reporting import DeployReporter
from skaal.deploy.targets.base import BuildOptions, DeployOptions, Target

if TYPE_CHECKING:
    from skaal.plan import PlanFile


def _generate_artifacts(
    app: Any,
    plan: "PlanFile",
    output_dir: Path,
    source_module: str,
    app_var: str,
    region: str,
    stack_profile: dict[str, Any] | None,
) -> list[Path]:
    from skaal.deploy.builders.gcp_stack import _build_pulumi_stack
    from skaal.deploy.runtime_assets import (
        collect_runtime_dependencies,
        copy_runtime_source_bundle,
        prepare_output_dir,
        write_pulumi_stack_artifact,
        write_pyproject_artifact,
        write_rendered_artifact,
        write_runtime_bootstrap,
    )

    output_dir = prepare_output_dir(output_dir)
    is_wsgi = bool(getattr(app, "_wsgi_attribute", None))

    generated = [
        write_runtime_bootstrap(
            output_dir,
            target="gcp",
            output_name="main.py",
            template_name="gcp/main",
            source_module=source_module,
            app_var=app_var,
            plan=plan,
            app=app,
            non_wsgi_context={"app_name": app.name},
        )
    ]

    bundles = copy_runtime_source_bundle(
        output_dir,
        project_root=output_dir.parent,
        source_module=source_module,
        include_source=True,
        include_mesh=True,
    )
    generated.extend(bundles.generated_paths)

    cmd_args = (
        "gunicorn --bind 0.0.0.0:8080 --workers 4 --timeout 120 main:application"
        if is_wsgi
        else "python main.py"
    )
    generated.append(
        write_rendered_artifact(output_dir, "Dockerfile", "gcp/Dockerfile", cmd_args=cmd_args)
    )

    base_dependency_sets = ["gcp-runtime"]
    if is_wsgi:
        base_dependency_sets.append("gcp-wsgi")
    else:
        base_dependency_sets.append("local-asgi")
    if bundles.has_mesh:
        base_dependency_sets.append("mesh-runtime")

    deps = collect_runtime_dependencies(
        plan,
        source_module,
        target="gcp",
        base_dependency_sets=base_dependency_sets,
    )
    generated.append(
        write_pyproject_artifact(
            output_dir,
            app_name=app.name,
            deps=deps,
            uv_sources=bundles.uv_sources,
        )
    )
    generated.append(
        write_pulumi_stack_artifact(
            output_dir,
            _build_pulumi_stack(app, plan, region, stack_profile),
        )
    )
    generated.append(
        write_meta(
            output_dir,
            target="gcp",
            source_module=source_module,
            app_name=app.name,
        )
    )
    return generated


class GCPCloudRunBuilder:
    def build(
        self,
        app: Any,
        plan: Any,
        output_dir: Path,
        source_module: str,
        app_var: str = "app",
        options: BuildOptions | None = None,
    ) -> list[Path]:
        resolved_options = options or BuildOptions()
        return _generate_artifacts(
            app=app,
            plan=plan,
            output_dir=output_dir,
            source_module=source_module,
            app_var=app_var,
            region=resolved_options.region or "us-central1",
            stack_profile=resolved_options.stack_profile,
        )


class GCPCloudRunDeployer:
    def deploy(
        self,
        artifacts_dir: Path,
        options: DeployOptions,
        reporter: DeployReporter | None = None,
    ) -> dict[str, str]:
        assert reporter is not None

        if not options.gcp_project:
            raise ValueError(
                "GCP project is required for the gcp target. "
                "Pass --gcp-project PROJECT or set SKAAL_GCP_PROJECT."
            )

        resolved_region = options.region or "us-central1"
        client = PulumiClient(artifacts_dir)
        client.select_or_init_stack(options.stack)
        client.config_set({"gcp:project": options.gcp_project, "gcp:region": resolved_region})
        if options.config_overrides:
            client.config_set(options.config_overrides)

        reporter.step("Provisioning GCP infrastructure")
        client.up(
            yes=options.yes,
            stage="provision GCP infrastructure",
            recovery_hint=(
                "Validate GCP credentials, region, and service APIs, then rerun the deploy."
            ),
        )

        repo = client.output("imageRepository")
        reporter.step(f"Building and pushing image to {repo}")
        try:
            build_push_image(
                artifacts_dir,
                project=options.gcp_project,
                region=resolved_region,
                repo=repo,
                app_name=options.app_name,
            )
        except DeployCommandError as exc:
            raise exc.with_recovery_hint(
                "The infrastructure stack was already created. Fix Docker or gcloud authentication "
                "and rerun the deploy, or roll the stack back manually if needed."
            ) from exc

        reporter.step("Deploying Cloud Run service revision")
        try:
            client.up(
                yes=options.yes,
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
