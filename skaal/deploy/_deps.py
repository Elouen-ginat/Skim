"""Auto-detect third-party dependencies from a user's source module.

When generating a deployment package Skaal needs to know which PyPI packages
the user's code imports so they can be added to ``requirements.txt``.

Rather than asking users to maintain a separate list, we parse their source
file with ``ast``, collect every top-level import name, filter out the stdlib
and Skaal itself, then map module names to distribution names using
``importlib.metadata.packages_distributions()``.

This means: if a user imports ``requests``, ``httpx``, or any other library,
it is automatically included in the generated ``requirements.txt`` without
any extra configuration.
"""

from __future__ import annotations

import ast
import importlib.metadata
import importlib.util
import sys
from pathlib import Path


def collect_user_packages(source_module: str) -> list[str]:
    """Return PyPI distribution names imported by *source_module*.

    The list is sorted and deduplicated.  Packages that are installed in the
    current environment are resolved to their canonical distribution name (e.g.
    ``dateutil`` → ``python-dateutil``, ``PIL`` → ``Pillow``).  Imports that
    cannot be resolved (not installed, private names) are silently skipped.

    Args:
        source_module: Dotted module path, e.g. ``"examples.02_todo_api.app"``.

    Returns:
        Sorted list of distribution names, e.g. ``["httpx", "pydantic"]``.
    """
    source_path = _find_source(source_module)
    if source_path is None:
        return []

    top_level_names = _ast_imports(source_path)
    return _resolve_packages(top_level_names)


# ── internals ─────────────────────────────────────────────────────────────────


def _find_source(module_name: str) -> Path | None:
    """Locate the .py file for *module_name*, or None if not found."""
    try:
        spec = importlib.util.find_spec(module_name)
    except (ModuleNotFoundError, ValueError):
        return None
    if spec is None or spec.origin is None:
        return None
    p = Path(spec.origin)
    return p if p.suffix == ".py" else None


def _ast_imports(source: Path) -> set[str]:
    """Return the set of top-level import names found in *source*."""
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
            # level > 0 → relative import; the package is the user's own code
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def _resolve_packages(names: set[str]) -> list[str]:
    """Map module names to distribution names, filtering stdlib and skaal."""
    stdlib = sys.stdlib_module_names  # available since Python 3.10

    # importlib.metadata.packages_distributions() → {module_name: [dist, ...]}
    pkg_dist: dict[str, list[str]] = dict(importlib.metadata.packages_distributions())

    distributions: set[str] = set()
    for name in names:
        if name in stdlib:
            continue
        if name == "skaal" or name.startswith("_"):
            continue
        dists = pkg_dist.get(name)
        if dists:
            distributions.add(dists[0])  # canonical (first) dist name

    return sorted(distributions)
