from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from skaal.deploy.backends import build_wiring_aws, collect_user_packages, get_handler
from skaal.deploy.builders.aws import build_pulumi_stack
from skaal.deploy.config import LambdaDeployConfig
from skaal.deploy.pulumi.automation import write_stack_spec
from skaal.deploy.pulumi.meta import write_meta
from skaal.deploy.pulumi.render import render, to_pulumi_yaml, to_pyproject_toml
from skaal.types import AppLike, StackProfile

if TYPE_CHECKING:
    from skaal.plan import PlanFile


def generate_artifacts(
    app: AppLike,
    plan: "PlanFile",
    output_dir: Path,
    source_module: str,
    app_var: str = "app",
    *,
    region: str | None = None,
    dev: bool = False,
    stack_profile: StackProfile | None = None,
) -> list[Path]:
    del dev
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    generated: list[Path] = []
    backend_imports, backend_overrides = build_wiring_aws(plan)
    wsgi_attribute: str | None = getattr(app, "_wsgi_attribute", None)
    enable_mesh = bool((stack_profile or {}).get("enable_mesh"))
    deploy_config = LambdaDeployConfig.model_validate(plan.deploy_config)

    handler_path = output_dir / "handler.py"
    if wsgi_attribute:
        handler_path.write_text(
            render(
                "aws/handler_wsgi.py",
                source_module=source_module,
                app_var=app_var,
                wsgi_attribute=wsgi_attribute,
                backend_imports=backend_imports,
                backend_overrides=backend_overrides,
            )
        )
    else:
        handler_path.write_text(
            render(
                "aws/handler.py",
                app_name=app.name,
                source_module=source_module,
                app_var=app_var,
                backend_imports=backend_imports,
                backend_overrides=backend_overrides,
            )
        )
    generated.append(handler_path)

    handler_extra_deps: list[str] = []
    for spec in plan.storage.values():
        for dependency in get_handler(spec).extra_deps:
            if dependency not in handler_extra_deps:
                handler_extra_deps.append(dependency)

    user_packages = collect_user_packages(source_module, project_root=output_dir.parent)
    base_deps = ["skaal[aws]"]
    if wsgi_attribute:
        base_deps.append("mangum>=0.17")
    if enable_mesh:
        base_deps.append("skaal-mesh")
    dependencies = list(dict.fromkeys(base_deps + handler_extra_deps + user_packages))

    pyproject_path = output_dir / "pyproject.toml"
    pyproject_path.write_text(to_pyproject_toml(app.name, dependencies), encoding="utf-8")
    generated.append(pyproject_path)

    stack = build_pulumi_stack(app, plan, region=region or "us-east-1")

    pulumi_yaml_path = output_dir / "Pulumi.yaml"
    pulumi_yaml_path.write_text(to_pulumi_yaml(stack), encoding="utf-8")
    generated.append(pulumi_yaml_path)

    generated.append(write_stack_spec(output_dir, stack))

    meta_path = write_meta(
        output_dir,
        target="aws",
        source_module=source_module,
        app_name=app.name,
        extra_fields={
            "lambda_architecture": deploy_config.architecture,
            "lambda_runtime": deploy_config.runtime,
        },
    )
    generated.append(meta_path)
    return generated
