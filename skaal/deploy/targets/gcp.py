from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from skaal.deploy.backends import build_wiring, collect_user_packages, get_handler
from skaal.deploy.builders.gcp import build_pulumi_stack
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
    import shutil

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    generated: list[Path] = []
    backend_imports, backend_overrides = build_wiring(plan)
    wsgi_attribute: str | None = getattr(app, "_wsgi_attribute", None)
    is_wsgi = bool(wsgi_attribute)
    project_root = output_dir.parent
    enable_mesh = bool((stack_profile or {}).get("enable_mesh"))

    main_path = output_dir / "main.py"
    if is_wsgi and wsgi_attribute is not None:
        main_path.write_text(
            render(
                "gcp/main_wsgi.py",
                source_module=source_module,
                app_var=app_var,
                wsgi_attribute=wsgi_attribute,
                backend_imports=backend_imports,
                backend_overrides=backend_overrides,
            )
        )
    else:
        main_path.write_text(
            render(
                "gcp/main.py",
                app_name=app.name,
                source_module=source_module,
                app_var=app_var,
                backend_imports=backend_imports,
                backend_overrides=backend_overrides,
            )
        )
    generated.append(main_path)

    top_package = source_module.split(".")[0]
    src_pkg_dir = project_root / top_package
    dst_pkg_dir = output_dir / top_package
    if src_pkg_dir.is_dir():
        shutil.copytree(src_pkg_dir, dst_pkg_dir, dirs_exist_ok=True)
        generated.append(dst_pkg_dir)

    cmd_args = (
        "gunicorn --bind 0.0.0.0:8080 --workers 4 --timeout 120 main:application"
        if is_wsgi
        else "python main.py"
    )
    dockerfile_path = output_dir / "Dockerfile"
    dockerfile_path.write_text(render("gcp/Dockerfile", cmd_args=cmd_args), encoding="utf-8")
    generated.append(dockerfile_path)

    infra_deps: list[str] = ["skaal[gcp]"]
    if is_wsgi:
        infra_deps.append("gunicorn>=22.0")
    else:
        infra_deps += ["uvicorn[standard]>=0.29", "starlette>=0.36"]
    if enable_mesh:
        infra_deps.append("skaal-mesh")
    seen_deps: set[str] = set()
    for spec in plan.storage.values():
        for dependency in get_handler(spec).extra_deps:
            if dependency not in seen_deps:
                seen_deps.add(dependency)
                infra_deps.append(dependency)
    dependencies = list(dict.fromkeys(infra_deps + collect_user_packages(source_module)))

    pyproject_path = output_dir / "pyproject.toml"
    pyproject_path.write_text(to_pyproject_toml(app.name, dependencies), encoding="utf-8")
    generated.append(pyproject_path)

    stack = build_pulumi_stack(app, plan, region or "us-central1", stack_profile)

    pulumi_yaml_path = output_dir / "Pulumi.yaml"
    pulumi_yaml_path.write_text(to_pulumi_yaml(stack), encoding="utf-8")
    generated.append(pulumi_yaml_path)

    generated.append(write_stack_spec(output_dir, stack))

    meta_path = write_meta(output_dir, target="gcp", source_module=source_module, app_name=app.name)
    generated.append(meta_path)
    return generated
