"""GCP Cloud Run artifact generator."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from skaal.deploy.builders.gcp_stack import _build_pulumi_stack
from skaal.deploy.runtime_assets import (
    collect_runtime_dependencies,
    copy_runtime_source_bundle,
    prepare_output_dir,
    write_meta_artifact,
    write_pulumi_stack_artifact,
    write_pyproject_artifact,
    write_rendered_artifact,
    write_runtime_bootstrap,
)

if TYPE_CHECKING:
    from skaal.plan import PlanFile


def generate_artifacts(
    app: Any,
    plan: "PlanFile",
    output_dir: Path,
    source_module: str,
    app_var: str = "app",
    region: str = "us-central1",
    stack_profile: dict[str, Any] | None = None,
) -> list[Path]:
    """Generate Cloud Run + Pulumi YAML deployment artifacts."""
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

    base_deps = ["skaal[gcp]"]
    if is_wsgi:
        base_deps.append("gunicorn>=22.0")
    else:
        base_deps.extend(["uvicorn[standard]>=0.29", "starlette>=0.36"])
    if bundles.has_mesh:
        base_deps.append("skaal-mesh")

    deps = collect_runtime_dependencies(
        plan,
        source_module,
        target="gcp",
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
    generated.append(
        write_pulumi_stack_artifact(
            output_dir,
            _build_pulumi_stack(app, plan, region, stack_profile),
        )
    )
    generated.append(
        write_meta_artifact(
            output_dir,
            target="gcp",
            source_module=source_module,
            app_name=app.name,
        )
    )
    return generated
