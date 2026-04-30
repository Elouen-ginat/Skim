"""Auto-detect third-party dependencies from a user's source module."""

from __future__ import annotations

import ast
import importlib.metadata
import importlib.util
import sys
import tomllib
from pathlib import Path
from typing import Any


def collect_user_packages(source_module: str, *, project_root: Path | None = None) -> list[str]:
    """Return PyPI distribution names imported by *source_module*."""
    declared_dependencies = _declared_build_dependencies(project_root or Path.cwd(), source_module)
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


def _declared_build_dependencies(project_root: Path, source_module: str) -> list[str]:
    pyproject = project_root / "pyproject.toml"
    if not pyproject.exists():
        return []

    try:
        with open(pyproject, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return []

    dependency_groups = data.get("dependency-groups", {})
    build_config = data.get("tool", {}).get("skaal", {}).get("build", {})
    if not isinstance(dependency_groups, dict) or not isinstance(build_config, dict):
        return []

    dependencies = _config_dependencies(build_config, dependency_groups)
    module_configs = build_config.get("modules", {})
    if isinstance(module_configs, dict):
        module_config = module_configs.get(source_module)
        if isinstance(module_config, dict):
            dependencies.extend(_config_dependencies(module_config, dependency_groups))
    return list(dict.fromkeys(dependencies))


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
