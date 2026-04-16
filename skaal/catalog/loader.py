"""Catalog loader — reads TOML catalog files and returns parsed Catalog objects.

Catalog sources, in order of precedence:

1. Explicit path (``--catalog ./my-catalog.toml``)
2. Short name registered via :func:`skaal.plugins.register_catalog` or the
   ``skaal.catalogs`` entry-point group (``--catalog aws`` → whatever an
   addon registers for ``aws``)
3. Built-in filesystem search (``catalogs/<target>.toml``)
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from skaal.catalog.models import Catalog
from skaal.errors import SkaalPluginError
from skaal.plugins import get_catalog_path

# Default search order when no explicit path is given.
# Cloud catalogs take priority; local.toml is the zero-setup fallback.
# The legacy "catalog/" path is kept for backward compat.
_DEFAULT_PATHS: list[str] = [
    "catalogs/aws.toml",
    "catalogs/gcp.toml",
    "catalogs/local.toml",
    "catalog/aws.toml",  # legacy path
]


def _resolve_path(path: Path | str | None, target: str | None) -> Path:
    """Turn a CLI ``--catalog`` argument into a concrete filesystem path.

    Accepts either an actual path or a short name registered via the
    ``skaal.catalogs`` entry-point group.  Raises ``FileNotFoundError`` when
    neither form resolves.
    """
    if path is not None:
        candidate = Path(path)
        if candidate.exists():
            return candidate
        # Not a filesystem path — try resolving it as a registered short name.
        try:
            named = get_catalog_path(str(path))
        except SkaalPluginError:
            named = None
        if named is not None and named.exists():
            return named
        raise FileNotFoundError(
            f"Catalog not found at {candidate} and no catalog registered under "
            f"the name {str(path)!r}. Pass --catalog <path> or ensure "
            "catalogs/aws.toml exists."
        )

    # No explicit --catalog: search the filesystem.
    search_order = _DEFAULT_PATHS.copy()
    if target and target not in ("generic",):
        base_target = target.split("-")[0]
        target_catalog = f"catalogs/{base_target}.toml"
        if target_catalog in search_order:
            search_order.remove(target_catalog)
        search_order.insert(0, target_catalog)

    for candidate_str in search_order:
        p = Path.cwd() / candidate_str
        if p.exists():
            return p

    # Last resort: ask the plugin registry for the target name.
    if target:
        try:
            plugin_path = get_catalog_path(target.split("-")[0])
        except SkaalPluginError:
            plugin_path = None
        if plugin_path is not None and plugin_path.exists():
            return plugin_path

    raise FileNotFoundError(
        "No catalog found. Tried: "
        + ", ".join(search_order)
        + ". Pass --catalog <path-or-name> or create catalogs/aws.toml."
    )


def load_catalog(path: Path | str | None = None, target: str | None = None) -> dict[str, Any]:
    """Load a catalog TOML and return the raw dict.

    Args:
        path: Explicit path to a catalog file, or a short name registered via
              :func:`skaal.plugins.register_catalog` / ``skaal.catalogs``
              entry points (e.g. ``"aws"`` from an ``skaal-aws`` addon).
        target: Deploy target name (e.g., 'aws', 'gcp', 'aws-lambda') used to
                bias filesystem search when *path* is not given.
    """
    resolved = _resolve_path(path, target)
    with open(resolved, "rb") as f:
        return tomllib.load(f)


def load_typed_catalog(path: Path | str | None = None, target: str | None = None) -> Catalog:
    """
    Load a catalog TOML and return a typed :class:`~skaal.catalog.models.Catalog`.

    This function automatically validates the catalog structure using Pydantic models.
    Missing required fields or incorrect types will raise a clear ValueError.

    Args:
        path:   Explicit path (or registered short name — see :func:`load_catalog`).
        target: Deploy target name (e.g., 'aws', 'gcp') for catalog selection.

    Raises:
        FileNotFoundError: If catalog file is not found.
        ValueError: If catalog structure is invalid or required fields are missing.

    Returns:
        A validated Catalog object.
    """
    try:
        raw = load_catalog(path, target=target)
        return Catalog.from_raw(raw)
    except ValueError as e:
        # Re-raise validation errors with better context
        raise ValueError(
            f"Invalid catalog structure: {e}. "
            "Check that required fields like read_latency.max are present in each backend entry."
        ) from e
