"""Catalog loader — reads TOML catalog files and returns parsed Catalog objects."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from skaal.catalog.models import Catalog

# Default search order when no explicit path is given
_DEFAULT_PATHS: list[str] = [
    "catalogs/aws.toml",
    "catalogs/local.toml",
    "catalog/aws.toml",  # legacy path, kept for backward compat
]


def load_catalog(path: Path | str | None = None) -> dict[str, Any]:
    """
    Load a catalog TOML and return the raw dict.

    Searches ``CWD/catalogs/aws.toml`` (then other defaults) when *path* is
    not given.  Raises ``FileNotFoundError`` if nothing is found.
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

    for candidate in _DEFAULT_PATHS:
        p = Path.cwd() / candidate
        if p.exists():
            with open(p, "rb") as f:
                return tomllib.load(f)

    raise FileNotFoundError(
        "No catalog found. Tried: "
        + ", ".join(_DEFAULT_PATHS)
        + ". Pass --catalog <path> or create catalogs/aws.toml."
    )


def load_typed_catalog(path: Path | str | None = None) -> Catalog:
    """
    Load a catalog TOML and return a typed :class:`~skaal.catalog.models.Catalog`.

    Tolerates unknown keys in backend entries; extra fields are accessible
    via ``catalog.raw``.
    """
    raw = load_catalog(path)
    return Catalog.from_raw(raw)
