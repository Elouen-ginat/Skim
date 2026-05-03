"""Auto-detect third-party dependencies from a user's source module."""

from __future__ import annotations

import ast
import importlib.metadata
import importlib.util
import sys
import tomllib
from pathlib import Path
from typing import Any, Iterable


def collect_user_packages(
    source_module: str,
    *,
    project_root: Path | None = None,
    target: str | None = None,
    variants: Iterable[str] = (),
    features: Iterable[str] = (),
) -> list[str]:
    """Return PyPI distribution names imported by *source_module*."""
    declared_dependencies = _declared_build_dependencies(
        project_root or Path.cwd(),
        source_module,
        target=target,
        variants=variants,
        features=features,
    )
    source_path = _find_source(source_module)
    if source_path is None:
        return declared_dependencies

    tree = _parse_source(source_path)
    if tree is None:
        return declared_dependencies

    top_level_names = _ast_imports(tree)
    dependencies = _resolve_packages(top_level_names)
    return list(dict.fromkeys(dependencies + declared_dependencies))


def _find_source(module_name: str) -> Path | None:
    try:
        spec = importlib.util.find_spec(module_name)
    except (ModuleNotFoundError, ValueError):
        return None
    if spec is None or spec.origin is None:
        return None
    path = Path(spec.origin)
    return path if path.suffix == ".py" else None


def _parse_source(source: Path) -> ast.Module | None:
    try:
        return ast.parse(source.read_text(encoding="utf-8"))
    except SyntaxError:
        return None


def _ast_imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def _declared_build_dependencies(
    project_root: Path,
    source_module: str,
    *,
    target: str | None = None,
    variants: Iterable[str] = (),
    features: Iterable[str] = (),
) -> list[str]:
    dependency_groups, build_config = _load_build_config(project_root)
    if not dependency_groups or not build_config:
        return []

    dependencies = _config_dependencies(build_config, dependency_groups)
    module_configs = build_config.get("modules", {})
    if isinstance(module_configs, dict):
        module_config = module_configs.get(source_module)
        if isinstance(module_config, dict):
            dependencies.extend(_config_dependencies(module_config, dependency_groups))
    if target is not None:
        target_configs = build_config.get("targets", {})
        if isinstance(target_configs, dict):
            target_config = target_configs.get(target)
            if isinstance(target_config, dict):
                dependencies.extend(_config_dependencies(target_config, dependency_groups))
                variant_configs = target_config.get("variants", {})
                if isinstance(variant_configs, dict):
                    for variant in variants:
                        variant_config = variant_configs.get(variant)
                        if isinstance(variant_config, dict):
                            dependencies.extend(
                                _config_dependencies(variant_config, dependency_groups)
                            )
    feature_configs = build_config.get("features", {})
    if isinstance(feature_configs, dict):
        for feature in features:
            feature_config = feature_configs.get(feature)
            if isinstance(feature_config, dict):
                dependencies.extend(_config_dependencies(feature_config, dependency_groups))
    return list(dict.fromkeys(dependencies))


def _load_build_config(project_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    merged_dependency_groups: dict[str, Any] = {}
    merged_build_config: dict[str, Any] = {}

    for pyproject in _candidate_pyprojects(project_root):
        try:
            with open(pyproject, "rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError):
            continue

        dependency_groups = data.get("dependency-groups", {})
        build_config = data.get("tool", {}).get("skaal", {}).get("build", {})
        if isinstance(dependency_groups, dict):
            merged_dependency_groups = _merge_dicts(merged_dependency_groups, dependency_groups)
        if isinstance(build_config, dict):
            merged_build_config = _merge_dicts(merged_build_config, build_config)

    return merged_dependency_groups, merged_build_config


def _candidate_pyprojects(project_root: Path) -> list[Path]:
    candidates: list[Path] = []
    default_pyproject = Path(__file__).resolve().parents[3] / "pyproject.toml"
    project_pyproject = project_root / "pyproject.toml"
    for path in (default_pyproject, project_pyproject):
        if path.exists() and path not in candidates:
            candidates.append(path)
    return candidates


def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _config_dependencies(config: dict[str, Any], dependency_groups: dict[str, Any]) -> list[str]:
    dependencies = _string_list(config.get("dependencies"))
    for group_name in _string_list(config.get("dependency_groups")):
        dependencies.extend(_string_list(dependency_groups.get(group_name)))
    return dependencies


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _resolve_packages(names: set[str]) -> list[str]:
    stdlib = sys.stdlib_module_names
    package_distributions: dict[str, list[str]] = dict(importlib.metadata.packages_distributions())

    distributions: set[str] = set()
    for name in names:
        if name in stdlib:
            continue
        if name == "skaal" or name.startswith("_"):
            continue
        resolved = package_distributions.get(name)
        if resolved:
            distributions.add(resolved[0])

    return sorted(distributions)
