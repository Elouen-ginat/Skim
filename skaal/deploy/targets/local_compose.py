"""Local Docker Compose deploy target — artifact generation and deploy."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from skaal.deploy.config import LocalStackDeployConfig
from skaal.deploy.pulumi import run_command
from skaal.deploy.push import write_meta
from skaal.deploy.reporting import DeployReporter
from skaal.deploy.targets.base import BuildOptions, DeployOptions, Target

if TYPE_CHECKING:
    from skaal.plan import PlanFile


_LOCAL_GITIGNORE = (
    "# Local data\n"
    "data/\n"
    "*.db\n"
    "*.sqlite3\n"
    "__pycache__/\n"
    ".pytest_cache/\n"
    ".env\n"
    ".env.local\n"
)


def _generate_artifacts(
    app: Any,
    plan: "PlanFile",
    output_dir: Path,
    source_module: str,
    app_var: str,
    dev: bool,
) -> list[Path]:
    from skaal.deploy.builders.local_compose import _build_docker_compose, build_kong_config
    from skaal.deploy.runtime_assets import (
        collect_runtime_dependencies,
        copy_runtime_source_bundle,
        prepare_output_dir,
        resolve_bootstrap_artifact,
        write_pyproject_artifact,
        write_rendered_artifact,
        write_runtime_bootstrap,
        write_text_artifact,
    )

    output_dir = prepare_output_dir(output_dir)
    deploy_config = LocalStackDeployConfig.model_validate(plan.deploy_config)
    is_wsgi = bool(getattr(app, "_wsgi_attribute", None))
    bootstrap = resolve_bootstrap_artifact(source_module, default_filename="main.py")

    generated = [
        write_runtime_bootstrap(
            output_dir,
            target="local",
            output_name=bootstrap.filename,
            template_name="local/main",
            source_module=source_module,
            app_var=app_var,
            plan=plan,
            app=app,
        )
    ]

    bundles = copy_runtime_source_bundle(
        output_dir,
        project_root=output_dir.parent,
        source_module=source_module,
        include_source=True,
        include_mesh=True,
        include_dev_skaal=dev,
    )
    generated.extend(bundles.generated_paths)
    source_entry = bundles.source_entry or source_module.split(".")[0]

    base_dependency_sets = ["local-compose"]
    if not is_wsgi:
        base_dependency_sets.append("local-asgi")
    if bundles.has_mesh:
        base_dependency_sets.append("mesh-runtime")

    deps = collect_runtime_dependencies(
        plan,
        source_module,
        target="local",
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

    gunicorn_worker = "" if is_wsgi else ', "-k", "uvicorn.workers.UvicornWorker"'
    generated.append(
        write_rendered_artifact(
            output_dir,
            "Dockerfile",
            "local/Dockerfile",
            bootstrap_module=bootstrap.module_name,
            gunicorn_worker=gunicorn_worker,
        )
    )

    kong_config = build_kong_config(
        app,
        plan,
        app_service_name=deploy_config.app_service_name,
    )
    if kong_config is not None:
        generated.append(write_text_artifact(output_dir, "kong.yml", kong_config))

    generated.append(
        write_text_artifact(
            output_dir,
            "docker-compose.yml",
            _build_docker_compose(
                plan,
                deploy_config.port,
                source_pkg=source_entry,
                app=app,
                dev=dev,
                is_wsgi=is_wsgi,
                bootstrap_module=bootstrap.module_name,
                app_service_name=deploy_config.app_service_name,
                app_container_name=deploy_config.container_name,
            ),
        )
    )
    generated.append(write_text_artifact(output_dir, ".gitignore", _LOCAL_GITIGNORE))
    generated.append(
        write_meta(
            output_dir,
            target="local",
            source_module=source_module,
            app_name=app.name,
        )
    )
    return generated


class LocalComposeBuilder:
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
            dev=resolved_options.dev,
        )


class LocalComposeDeployer:
    def deploy(
        self,
        artifacts_dir: Path,
        options: DeployOptions,
        reporter: DeployReporter | None = None,
    ) -> dict[str, str]:
        assert reporter is not None

        detach = bool((options.runtime_options or {}).get("detach", False))
        follow_logs = bool((options.runtime_options or {}).get("follow_logs", False))

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
