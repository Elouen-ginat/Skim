"""AWS Lambda artifact generator."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from skaal.deploy.builders.aws_stack import _build_pulumi_stack
from skaal.deploy.runtime_assets import (
    collect_runtime_dependencies,
    prepare_output_dir,
    project_has_mesh,
    write_meta_artifact,
    write_pulumi_stack_artifact,
    write_pyproject_artifact,
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
    region: str = "us-east-1",
) -> list[Path]:
    """Generate Lambda + Pulumi YAML deployment artifacts."""
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

    base_deps = ["skaal[aws]"]
    if getattr(app, "_wsgi_attribute", None):
        base_deps.append("mangum>=0.17")
    if project_has_mesh(output_dir.parent):
        base_deps.append("skaal-mesh")

    deps = collect_runtime_dependencies(
        plan,
        source_module,
        target="aws",
        base_deps=base_deps,
    )
    generated.append(write_pyproject_artifact(output_dir, app_name=app.name, deps=deps))
    generated.append(
        write_pulumi_stack_artifact(output_dir, _build_pulumi_stack(app, plan, region=region))
    )
    generated.append(
        write_meta_artifact(
            output_dir,
            target="aws",
            source_module=source_module,
            app_name=app.name,
        )
    )
    return generated
