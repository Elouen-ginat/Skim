"""Catalog loader — reads TOML catalog files and returns parsed Catalog objects."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from skaal.catalog.models import Catalog

# Default search order when no explicit path is given.
# Cloud catalogs take priority; local.toml is the zero-setup fallback.
# The legacy "catalog/" path is kept for backward compat.
_DEFAULT_PATHS: list[str] = [
    "catalogs/aws.toml",
    "catalogs/gcp.toml",
    "catalogs/local.toml",
    "catalog/aws.toml",  # legacy path
]


def load_catalog(path: Path | str | None = None, target: str | None = None) -> dict[str, Any]:
    """
    Load a catalog TOML and return the raw dict.

    Searches ``CWD/catalogs/<target>.toml`` (or catalogs/aws.toml if target is
    not given) when *path* is not given.  Raises ``FileNotFoundError`` if nothing
    is found.

    Args:
        path: Explicit path to catalog file. If given, target is ignored.
        target: Deploy target name to search for (e.g., 'aws', 'gcp', 'aws-lambda').
                Base target extracted from full target name (e.g., 'aws' from 'aws-lambda').
    """
    if path is not None:
        resolved = Path(path)
        if not resolved.exists():
            raise FileNotFoundError(
                f"Catalog not found at {resolved}. "
                "Pass --catalog <path> or ensure catalogs/aws.toml exists."
            )
        with open(resolved, "rb") as f:
            return tomllib.load(f)

    # Build search order: prioritize target-specific catalog, then cloud catalogs, then local
    search_order = _DEFAULT_PATHS.copy()
    if target and target not in ("generic",):
        # Extract base target from full target name (e.g., 'aws' from 'aws-lambda')
        base_target = target.split("-")[0]
        target_catalog = f"catalogs/{base_target}.toml"
        if target_catalog in search_order:
            search_order.remove(target_catalog)
        search_order.insert(0, target_catalog)

    for candidate in search_order:
        p = Path.cwd() / candidate
        if p.exists():
            with open(p, "rb") as f:
                return tomllib.load(f)

    raise FileNotFoundError(
        "No catalog found. Tried: "
        + ", ".join(search_order)
        + ". Pass --catalog <path> or create catalogs/aws.toml."
    )


def load_typed_catalog(path: Path | str | None = None, target: str | None = None) -> Catalog:
    """
    Load a catalog TOML and return a typed :class:`~skaal.catalog.models.Catalog`.

    This function automatically validates the catalog structure using Pydantic models.
    Missing required fields or incorrect types will raise a clear ValueError.

    Args:
        path:   Explicit path to catalog file. If given, target is ignored.
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
