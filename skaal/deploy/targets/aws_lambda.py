"""AWS Lambda deploy target — artifact generation, packaging, and deploy."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from skaal.deploy.pulumi import PulumiClient
from skaal.deploy.push import package_aws, write_meta
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
) -> list[Path]:
    from skaal.deploy.builders.aws_stack import _build_pulumi_stack
    from skaal.deploy.runtime_assets import (
        collect_runtime_dependencies,
        prepare_output_dir,
        project_has_mesh,
        write_pulumi_stack_artifact,
        write_pyproject_artifact,
        write_runtime_bootstrap,
    )

    output_dir = prepare_output_dir(output_dir)

    generated = [
        write_runtime_bootstrap(
            output_dir,
            target="aws",
            output_name="handler.py",
            template_name="aws/handler",
            source_module=source_module,
            app_var=app_var,
            plan=plan,
            app=app,
            non_wsgi_context={"app_name": app.name},
        )
    ]

    base_dependency_sets = ["aws-runtime"]
    if getattr(app, "_wsgi_attribute", None):
        base_dependency_sets.append("aws-wsgi")
    if project_has_mesh(output_dir.parent):
        base_dependency_sets.append("mesh-runtime")

    deps = collect_runtime_dependencies(
        plan,
        source_module,
        target="aws",
        base_dependency_sets=base_dependency_sets,
    )
    generated.append(write_pyproject_artifact(output_dir, app_name=app.name, deps=deps))
    generated.append(
        write_pulumi_stack_artifact(output_dir, _build_pulumi_stack(app, plan, region=region))
    )
    generated.append(
        write_meta(
            output_dir,
            target="aws",
            source_module=source_module,
            app_name=app.name,
        )
    )
    return generated


class AWSLambdaBuilder:
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
            region=resolved_options.region or "us-east-1",
        )


class AWSLambdaDeployer:
    def deploy(
        self,
        artifacts_dir: Path,
        options: DeployOptions,
        reporter: DeployReporter | None = None,
    ) -> dict[str, str]:
        assert reporter is not None

        client = PulumiClient(artifacts_dir)
        client.select_or_init_stack(options.stack)
        client.config_set({"aws:region": options.region or "us-east-1"})
        if options.config_overrides:
            client.config_set(options.config_overrides)

        reporter.step("Packaging Lambda")
        package_aws(artifacts_dir, options.project_root, options.source_module)

        reporter.step("Deploying AWS Lambda stack")
        client.up(
            yes=options.yes,
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
