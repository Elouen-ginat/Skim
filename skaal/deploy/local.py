"""Local Docker Compose artifact generator."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from skaal.deploy.builders.local_compose import _build_docker_compose, build_kong_config
from skaal.deploy.config import LocalStackDeployConfig
from skaal.deploy.runtime_assets import (
    collect_runtime_dependencies,
    copy_runtime_source_bundle,
    prepare_output_dir,
    write_meta_artifact,
    write_pyproject_artifact,
    write_rendered_artifact,
    write_runtime_bootstrap,
    write_text_artifact,
)

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


def generate_artifacts(
    app: Any,
    plan: "PlanFile",
    output_dir: Path,
    source_module: str,
    app_var: str = "app",
    dev: bool = False,
) -> list[Path]:
    """Generate Docker Compose deployment artifacts."""
    output_dir = prepare_output_dir(output_dir)
    deploy_config = LocalStackDeployConfig.model_validate(plan.deploy_config)
    is_wsgi = bool(getattr(app, "_wsgi_attribute", None))
    top_pkg = source_module.split(".")[0]

    generated = [
        write_runtime_bootstrap(
            output_dir,
            target="local",
            output_name="main.py",
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

    base_deps = ["skaal", "gunicorn>=22.0", "apscheduler>=3.10"]
    if not is_wsgi:
        base_deps.extend(["uvicorn[standard]>=0.29", "starlette>=0.36"])
    if bundles.has_mesh:
        base_deps.append("skaal-mesh")

    deps = collect_runtime_dependencies(
        plan,
        source_module,
        target="local",
        base_deps=base_deps,
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
                source_pkg=top_pkg,
                app=app,
                dev=dev,
                is_wsgi=is_wsgi,
                app_service_name=deploy_config.app_service_name,
                app_container_name=deploy_config.container_name,
            ),
        )
    )
    generated.append(write_text_artifact(output_dir, ".gitignore", _LOCAL_GITIGNORE))
    generated.append(
        write_meta_artifact(
            output_dir,
            target="local",
            source_module=source_module,
            app_name=app.name,
        )
    )
    return generated
