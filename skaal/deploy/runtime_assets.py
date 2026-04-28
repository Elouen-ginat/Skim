"""Shared helpers for deploy artifact assembly."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from skaal.deploy._deps import collect_user_packages
from skaal.deploy._render import render, to_pulumi_yaml, to_pyproject_toml
from skaal.deploy.dependencies import resolve_dependency_sets
from skaal.deploy.packaging.source_bundle import (
    copy_dev_skaal_bundle,
    copy_mesh_bundle,
    copy_source_package,
)
from skaal.deploy.wiring import resolve_backend

if TYPE_CHECKING:
    from skaal.plan import PlanFile


@dataclass(slots=True)
class SourceBundleResult:
    """Files and package source overrides copied into a deploy artifact."""

    generated_paths: list[Path] = field(default_factory=list)
    has_mesh: bool = False
    source_entry: str | None = None
    uv_sources: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BootstrapArtifact:
    filename: str
    module_name: str


def prepare_output_dir(output_dir: Path) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def project_has_mesh(project_root: Path) -> bool:
    mesh_src_dir = Path(project_root) / "mesh"
    return mesh_src_dir.is_dir() and (mesh_src_dir / "Cargo.toml").exists()


def resolve_bootstrap_artifact(source_module: str, *, default_filename: str) -> BootstrapArtifact:
    default_module = Path(default_filename).stem
    if "." not in source_module and source_module == default_module:
        return BootstrapArtifact(filename="_skaal_bootstrap.py", module_name="_skaal_bootstrap")
    return BootstrapArtifact(filename=default_filename, module_name=default_module)


def write_text_artifact(
    output_dir: Path,
    filename: str,
    content: str,
    *,
    encoding: str = "utf-8",
) -> Path:
    path = output_dir / filename
    path.write_text(content, encoding=encoding)
    return path


def write_rendered_artifact(
    output_dir: Path,
    filename: str,
    template_name: str,
    *,
    encoding: str = "utf-8",
    **context: Any,
) -> Path:
    return write_text_artifact(
        output_dir,
        filename,
        render(template_name, **context),
        encoding=encoding,
    )


def write_runtime_bootstrap(
    output_dir: Path,
    *,
    target: str,
    output_name: str,
    template_name: str,
    source_module: str,
    app_var: str,
    plan: "PlanFile",
    app: Any,
    non_wsgi_context: dict[str, Any] | None = None,
) -> Path:
    wsgi_attribute: str | None = getattr(app, "_wsgi_attribute", None)

    context: dict[str, Any] = {
        "source_module": source_module,
        "app_var": app_var,
        "plan_json_literal": repr(plan.model_dump_json()),
        "target_name": target,
    }
    if wsgi_attribute:
        context["wsgi_attribute"] = wsgi_attribute
        selected_template = f"{template_name}_wsgi.py"
    else:
        context.update(non_wsgi_context or {})
        selected_template = f"{template_name}.py"

    return write_rendered_artifact(output_dir, output_name, selected_template, **context)


def collect_runtime_dependencies(
    plan: "PlanFile",
    source_module: str,
    *,
    target: str,
    base_dependency_sets: list[str],
) -> list[str]:
    deps = resolve_dependency_sets(base_dependency_sets)
    seen = set(deps)

    for spec in plan.storage.values():
        wiring = resolve_backend(spec, target=target).wiring
        for dep in resolve_dependency_sets(wiring.dependency_sets):
            if dep not in seen:
                deps.append(dep)
                seen.add(dep)
        for dep in wiring.dependencies:
            if dep not in seen:
                deps.append(dep)
                seen.add(dep)

    for dep in collect_user_packages(source_module):
        if dep not in seen:
            deps.append(dep)
            seen.add(dep)

    return deps


def write_pyproject_artifact(
    output_dir: Path,
    *,
    app_name: str,
    deps: list[str],
    uv_sources: dict[str, str] | None = None,
) -> Path:
    return write_text_artifact(
        output_dir,
        "pyproject.toml",
        to_pyproject_toml(app_name, deps, uv_sources=uv_sources or None),
    )


def write_pulumi_stack_artifact(output_dir: Path, stack: dict[str, Any]) -> Path:
    return write_text_artifact(output_dir, "Pulumi.yaml", to_pulumi_yaml(stack))


def copy_runtime_source_bundle(
    output_dir: Path,
    *,
    project_root: Path,
    source_module: str,
    include_source: bool,
    include_mesh: bool,
    include_dev_skaal: bool = False,
) -> SourceBundleResult:
    result = SourceBundleResult()

    if include_dev_skaal:
        skaal_bundle_dir = copy_dev_skaal_bundle(output_dir, project_root=project_root)
        if skaal_bundle_dir is not None:
            result.generated_paths.append(skaal_bundle_dir)
            result.uv_sources["skaal"] = "./_skaal"

    if include_mesh:
        mesh_bundle_dir = copy_mesh_bundle(output_dir, project_root=project_root)
        if mesh_bundle_dir is not None:
            result.generated_paths.append(mesh_bundle_dir)
            result.has_mesh = True
            result.uv_sources["skaal-mesh"] = "./mesh"

    if include_source:
        source_bundle = copy_source_package(
            output_dir,
            project_root=project_root,
            source_module=source_module,
        )
        if source_bundle is not None:
            result.generated_paths.append(source_bundle)
            result.source_entry = source_bundle.name

    return result
