from __future__ import annotations

import platform
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from skaal.deploy.backends import build_wiring, collect_user_packages, get_handler
from skaal.deploy.builders.common import resource_slug
from skaal.deploy.builders.local import build_kong_config, build_pulumi_stack
from skaal.deploy.pulumi.automation import write_stack_spec
from skaal.deploy.pulumi.meta import write_meta
from skaal.deploy.pulumi.render import render, to_pulumi_yaml, to_pyproject_toml
from skaal.types import AppLike, StackProfile

if TYPE_CHECKING:
    from skaal.plan import PlanFile


_LOCAL_GITIGNORE = ".pulumi/\n.pulumi-state/\n__pycache__/\n.pytest_cache/\n.env\n.env.local\n"
_LOCAL_DOCKERIGNORE = ".pulumi/\n.pulumi-state/\n__pycache__/\n.pytest_cache/\n"


def _mesh_docker_stages(build_in_docker: bool) -> tuple[str, str]:
    if not build_in_docker:
        return "", ""
    build_stage = (
        "FROM rust:1-slim-bookworm AS mesh-builder\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends python3-dev python3-pip && rm -rf /var/lib/apt/lists/*\n"
        "RUN pip install --break-system-packages --quiet maturin\n"
        "COPY _mesh_src/ /build/\n"
        "WORKDIR /build\n"
        "RUN maturin build --manifest-path mesh/Cargo.toml --release --out /dist\n"
    )
    install_step = (
        "\n# Install compiled skaal-mesh extension into the uv-managed venv.\n"
        "COPY --from=mesh-builder /dist/ /tmp/mesh_wheels/\n"
        "RUN uv pip install --no-cache-dir /tmp/mesh_wheels/*.whl && rm -rf /app/_mesh_src /tmp/mesh_wheels\n"
    )
    return build_stage, install_step


def _source_uses_mesh(source_module: str, project_root: Path) -> bool:
    parts = source_module.split(".")
    scan_dir: Path | None = None
    for depth in range(len(parts), 0, -1):
        candidate = project_root.joinpath(*parts[:depth])
        if candidate.is_dir():
            scan_dir = candidate
            break
    if scan_dir is None:
        return False
    for py_file in scan_dir.rglob("*.py"):
        try:
            content = py_file.read_text(encoding="utf-8", errors="ignore")
            if "skaal.mesh" in content or "skaal_mesh" in content:
                return True
        except OSError:
            pass
    return False


def _copy_mesh_source(project_root: Path, output_dir: Path) -> None:
    dst = output_dir / "_mesh_src"
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir()
    for filename in ("Cargo.toml", "Cargo.lock"):
        src = project_root / filename
        if src.exists():
            shutil.copy2(src, dst / filename)
    shutil.copytree(
        project_root / "mesh",
        dst / "mesh",
        ignore=shutil.ignore_patterns("target", "__pycache__", "*.pyc"),
    )


def _local_mesh_wheel_pattern() -> re.Pattern[str]:
    machine = platform.machine().lower()
    arch = "aarch64" if machine in {"arm64", "aarch64"} else "x86_64"
    return re.compile(rf"^skaal_mesh-.*-manylinux[^-]*_{arch}\.whl$")


def _bundle_local_mesh_wheel(project_root: Path, output_dir: Path) -> str | None:
    wheel_pattern = _local_mesh_wheel_pattern()
    candidate_dirs = [project_root / "target" / "wheels", project_root / "mesh" / "dist"]

    for wheel_dir in candidate_dirs:
        if not wheel_dir.is_dir():
            continue
        matches = sorted(
            path
            for path in wheel_dir.iterdir()
            if path.is_file() and wheel_pattern.match(path.name)
        )
        if not matches:
            continue
        bundled_dir = output_dir / "_mesh_wheels"
        bundled_dir.mkdir(exist_ok=True)
        wheel_path = matches[-1]
        shutil.copy2(wheel_path, bundled_dir / wheel_path.name)
        return f"skaal-mesh @ file:///app/_mesh_wheels/{wheel_path.name}"

    return None


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
    del region
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    top_package = source_module.split(".")[0]
    project_root = output_dir.parent
    generated: list[Path] = []
    backend_imports, backend_overrides = build_wiring(plan, local=True)
    wsgi_attribute: str | None = getattr(app, "_wsgi_attribute", None)
    is_wsgi = bool(wsgi_attribute)
    enable_mesh = bool((stack_profile or {}).get("enable_mesh")) or _source_uses_mesh(
        source_module, project_root
    )

    main_path = output_dir / "main.py"
    if wsgi_attribute:
        main_path.write_text(
            render(
                "local/main_wsgi.py",
                source_module=source_module,
                app_var=app_var,
                wsgi_attribute=wsgi_attribute,
                backend_imports=backend_imports,
                backend_overrides=backend_overrides,
            ),
            encoding="utf-8",
        )
    else:
        main_path.write_text(
            render(
                "local/main.py",
                source_module=source_module,
                app_var=app_var,
                backend_imports=backend_imports,
                backend_overrides=backend_overrides,
            ),
            encoding="utf-8",
        )
    generated.append(main_path)

    skaal_bundle_dir = output_dir / "_skaal"
    skaal_src_dir = project_root / "skaal"
    skaal_root_pyproject = project_root / "pyproject.toml"
    if dev and skaal_src_dir.is_dir() and skaal_root_pyproject.exists():
        skaal_bundle_dir.mkdir(exist_ok=True)
        shutil.copytree(skaal_src_dir, skaal_bundle_dir / "skaal", dirs_exist_ok=True)
        shutil.copy2(skaal_root_pyproject, skaal_bundle_dir / "pyproject.toml")
        for extra in ("LICENSE", "README.md"):
            src = project_root / extra
            if src.exists():
                shutil.copy2(src, skaal_bundle_dir / extra)
        generated.append(skaal_bundle_dir)

    build_mesh_in_docker = False
    mesh_dep: str | None = None
    extra_features: list[str] = []
    if enable_mesh:
        mesh_dep = _bundle_local_mesh_wheel(project_root, output_dir) if dev else None
        if mesh_dep:
            extra_features = []
        elif dev and (project_root / "mesh" / "Cargo.toml").exists():
            build_mesh_in_docker = True
            _copy_mesh_source(project_root, output_dir)
            generated.append(output_dir / "_mesh_src")
        else:
            extra_features.append("mesh")
    seen_deps: set[str] = set()
    declared_deps = collect_user_packages(
        source_module,
        project_root=project_root,
        target="local",
        features=extra_features,
    )
    dependencies = list(dict.fromkeys(declared_deps + ([mesh_dep] if mesh_dep else [])))
    for spec in plan.storage.values():
        for dependency in get_handler(spec, local=True).extra_deps:
            if dependency not in seen_deps:
                seen_deps.add(dependency)
                dependencies.append(dependency)
    uv_sources: dict[str, str] = {}
    if dev and skaal_src_dir.is_dir():
        uv_sources["skaal"] = "./_skaal"
    pyproject_path = output_dir / "pyproject.toml"
    pyproject_path.write_text(
        to_pyproject_toml(app.name, dependencies, uv_sources=uv_sources or None),
        encoding="utf-8",
    )
    generated.append(pyproject_path)

    gunicorn_worker = "" if is_wsgi else ', "-k", "uvicorn.workers.UvicornWorker"'
    mesh_build_stage, mesh_install_step = _mesh_docker_stages(build_mesh_in_docker)
    dockerfile_path = output_dir / "Dockerfile"
    dockerfile_path.write_text(
        render(
            "local/Dockerfile",
            gunicorn_worker=gunicorn_worker,
            mesh_build_stage=mesh_build_stage,
            mesh_install_step=mesh_install_step,
        ),
        encoding="utf-8",
    )
    generated.append(dockerfile_path)

    src_pkg_dir = project_root / top_package
    dst_pkg_dir = output_dir / top_package
    if src_pkg_dir.is_dir():
        shutil.copytree(src_pkg_dir, dst_pkg_dir, dirs_exist_ok=True)
        generated.append(dst_pkg_dir)

    kong_app_service = resource_slug(app.name)
    kong_config = build_kong_config(app, plan, app_service_name=f"skaal-{kong_app_service}-app")
    if kong_config is not None:
        kong_path = output_dir / "kong.yml"
        kong_path.write_text(kong_config, encoding="utf-8")
        generated.append(kong_path)

    local_stack = build_pulumi_stack(
        app,
        plan,
        output_dir=output_dir,
        source_module=source_module,
        dev=dev,
    )
    pulumi_yaml_path = output_dir / "Pulumi.yaml"
    pulumi_yaml_path.write_text(to_pulumi_yaml(local_stack), encoding="utf-8")
    generated.append(pulumi_yaml_path)

    local_stack_spec_path = write_stack_spec(output_dir, local_stack)
    generated.append(local_stack_spec_path)

    gitignore_path = output_dir / ".gitignore"
    gitignore_path.write_text(_LOCAL_GITIGNORE, encoding="utf-8")
    generated.append(gitignore_path)

    dockerignore_path = output_dir / ".dockerignore"
    dockerignore_path.write_text(_LOCAL_DOCKERIGNORE, encoding="utf-8")
    generated.append(dockerignore_path)

    meta_path = write_meta(
        output_dir, target="local", source_module=source_module, app_name=app.name
    )
    generated.append(meta_path)
    return generated
