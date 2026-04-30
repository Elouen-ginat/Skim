"""Auto-detect third-party dependencies from a user's source module."""

from __future__ import annotations

import ast
import importlib.metadata
import importlib.util
import sys
from pathlib import Path


def collect_user_packages(source_module: str) -> list[str]:
    """Return PyPI distribution names imported by *source_module*."""
    source_path = _find_source(source_module)
    if source_path is None:
        return []

    top_level_names = _ast_imports(source_path)
    return _resolve_packages(top_level_names)


def _find_source(module_name: str) -> Path | None:
    try:
        spec = importlib.util.find_spec(module_name)
    except (ModuleNotFoundError, ValueError):
        return None
    if spec is None or spec.origin is None:
        return None
    path = Path(spec.origin)
    return path if path.suffix == ".py" else None


def _ast_imports(source: Path) -> set[str]:
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"))
    except SyntaxError:
        return set()

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


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
