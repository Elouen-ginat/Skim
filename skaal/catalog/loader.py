"""Catalog loader — reads TOML catalog files and returns parsed Catalog objects.

Catalog sources, in order of precedence:

1. Explicit path (``--catalog ./my-catalog.toml``)
2. Short name registered via the ``skaal.catalogs`` entry-point group
    (``--catalog aws`` → whatever an addon registers for ``aws``)
3. Built-in filesystem search (``catalogs/<target>.toml``)

Per ADR 022 each catalog file may declare a parent under ``[skaal] extends``
(path or registered short name) and prune entries from the merged result via
``[skaal] remove = ["storage.X"]``.  See :class:`skaal.types.CatalogSource`
for the introspectable resolved chain.
"""

from __future__ import annotations

import importlib.resources
import tomllib
from pathlib import Path
from typing import Any

from skaal.catalog.models import Catalog
from skaal.errors import CatalogError, SkaalPluginError
from skaal.plugins import get_catalog_path
from skaal.types.catalog import CatalogRaw, CatalogSource

# Sections we recursively merge by per-key (e.g. backend name) replacement.
# Keys outside this set are passed through with the child value winning.
_MERGE_SECTIONS: tuple[str, ...] = ("storage", "compute", "network")

# Reserved top-level table — not part of the merged catalog payload.
_RESERVED_TABLE: str = "skaal"

# Default search order when no explicit path is given.
# Cloud catalogs take priority; local.toml is the zero-setup fallback.
# The legacy "catalog/" path is kept for backward compat.
_DEFAULT_PATHS: list[str] = [
    "catalogs/aws.toml",
    "catalogs/gcp.toml",
    "catalogs/local.toml",
    "catalog/aws.toml",  # legacy path
]

# Catalog names bundled with the package (in skaal/catalog/data/).
_BUNDLED_CATALOGS: list[str] = ["aws.toml", "gcp.toml", "local.toml"]


def _load_bundled(name: str) -> dict[str, Any]:
    """Load a catalog TOML bundled inside the skaal package."""
    from skaal.errors import CatalogError

    data_pkg = importlib.resources.files("skaal.catalog.data")
    content = (data_pkg / name).read_bytes()
    try:
        return tomllib.loads(content.decode())
    except tomllib.TOMLDecodeError as err:  # pragma: no cover - bundled is valid
        raise CatalogError(f"bundled catalog {name!r}: invalid TOML: {err}") from err


def _resolve_path(path: Path | str | None, target: str | None) -> Path | dict[str, Any]:
    """Turn a CLI ``--catalog`` argument into a concrete filesystem path.

    Accepts either an actual path or a short name registered via the
    ``skaal.catalogs`` entry-point group.

    Search order:
    1. Explicit *path* (if given) — resolved as a filesystem path, then as a
       registered short name.
    2. ``CWD/catalogs/<target>.toml`` (and other CWD candidates).
    3. A plugin-registered catalog for *target*.
    4. Bundled catalog shipped with the skaal package (``skaal/catalog/data/``).

    This means ``skaal catalog`` works out-of-the-box after installation even
    without any local catalog files.  A project-local catalog always takes
    precedence over the bundled defaults.

    Args:
        path: Explicit path to catalog file. If given, target is ignored.
        target: Deploy target name to search for (e.g., 'aws', 'gcp', 'aws-lambda').
                Base target extracted from full target name (e.g., 'aws' from 'aws-lambda').

    Raises:
        FileNotFoundError: When neither form resolves.
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

    # 1. Try CWD-relative paths first (project-local catalog overrides bundled)
    for rel in search_order:
        p = Path.cwd() / rel
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

    # 2. Fall back to catalog bundled with the package
    bundled_name: str | None = None
    if target and target not in ("generic",):
        base_target = target.split("-")[0]
        candidate_name = f"{base_target}.toml"
        if candidate_name in _BUNDLED_CATALOGS:
            bundled_name = candidate_name
    if bundled_name is None:
        # Default to aws, then local as final fallback
        for name in ("aws.toml", "local.toml"):
            if name in _BUNDLED_CATALOGS:
                bundled_name = name
                break

    if bundled_name is not None:
        try:
            return _load_bundled(bundled_name)
        except (FileNotFoundError, ModuleNotFoundError):
            pass

    raise FileNotFoundError(
        "No catalog found. Tried: "
        + ", ".join(search_order)
        + ". Pass --catalog <path-or-name> or create catalogs/aws.toml."
    )


def _read_toml(p: Path) -> CatalogRaw:
    """Parse a catalog TOML, wrapping decode errors as :class:`CatalogError`."""
    try:
        with open(p, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as err:
        raise CatalogError(f"catalog {p}: invalid TOML: {err}") from err


def _split_skaal_table(
    raw: CatalogRaw,
) -> tuple[CatalogRaw, str | None, tuple[str, ...]]:
    """Pop the reserved ``[skaal]`` table off *raw*.

    Returns ``(payload, extends, removes)`` where:
      - *payload*  is the catalog dict with ``[skaal]`` removed.
      - *extends*  is the value of ``[skaal] extends`` or ``None``.
      - *removes*  is the value of ``[skaal] remove`` (always a tuple).
    """
    if _RESERVED_TABLE not in raw:
        return raw, None, ()
    reserved = raw[_RESERVED_TABLE]
    if not isinstance(reserved, dict):
        raise CatalogError(f"[{_RESERVED_TABLE}] must be a table; got {type(reserved).__name__}.")
    extends_value = reserved.get("extends")
    if extends_value is not None and not isinstance(extends_value, str):
        raise CatalogError(
            f"[{_RESERVED_TABLE}] extends must be a string; got {type(extends_value).__name__}."
        )
    removes_value = reserved.get("remove", [])
    if not isinstance(removes_value, list) or not all(isinstance(x, str) for x in removes_value):
        raise CatalogError(f"[{_RESERVED_TABLE}] remove must be a list[str].")
    payload = {k: v for k, v in raw.items() if k != _RESERVED_TABLE}
    return payload, extends_value, tuple(removes_value)


def _resolve_extends_path(extends: str, anchor: Path | None) -> tuple[Path | None, CatalogRaw]:
    """Resolve a parent reference.

    *extends* may be a relative/absolute filesystem path or a short name
    registered via the ``skaal.catalogs`` entry-point group / bundled
    catalog set.

    Returns ``(path, raw)`` — *path* is ``None`` when the parent was loaded
    from a bundled importlib resource, in which case *raw* carries the
    parsed payload directly.
    """
    if anchor is not None and not Path(extends).is_absolute():
        candidate = (anchor.parent / extends).resolve()
    else:
        candidate = Path(extends).resolve()
    if candidate.exists():
        return candidate, _read_toml(candidate)

    # Plugin-registered short name?
    try:
        plugin_path = get_catalog_path(extends)
    except SkaalPluginError:
        plugin_path = None
    if plugin_path is not None and plugin_path.exists():
        return plugin_path, _read_toml(plugin_path)

    # Bundled catalog short name?
    bundled_name = extends if extends.endswith(".toml") else f"{extends}.toml"
    if bundled_name in _BUNDLED_CATALOGS:
        return None, _load_bundled(bundled_name)

    raise CatalogError(
        f"[{_RESERVED_TABLE}] extends={extends!r} does not resolve to a file or "
        "a registered/bundled catalog short name."
    )


def _build_source_chain(
    raw: CatalogRaw,
    *,
    path: Path | None,
    visited: tuple[Path, ...] = (),
) -> CatalogSource:
    """Walk ``[skaal] extends`` parents depth-first, returning the leaf source."""
    payload, extends, removes = _split_skaal_table(raw)
    if path is not None:
        path = path.resolve()
        if path in visited:
            cycle = " → ".join(str(p) for p in (*visited, path))
            raise CatalogError(f"circular extends: {cycle}")
        visited = (*visited, path)

    parent_source: CatalogSource | None = None
    if extends is not None:
        parent_path, parent_raw = _resolve_extends_path(extends, anchor=path)
        parent_source = _build_source_chain(parent_raw, path=parent_path, visited=visited)

    return CatalogSource(path=path, raw=payload, parent=parent_source, removes=removes)


def _merge_payloads(parent: CatalogRaw, child: CatalogRaw) -> CatalogRaw:
    """Deep-merge two catalog payloads at section granularity.

    Child entries replace parent entries by key (per-backend granularity).
    """
    keys = set(parent) | set(child)
    merged: CatalogRaw = {}
    for key in keys:
        if key in _MERGE_SECTIONS:
            section_parent = parent.get(key, {})
            section_child = child.get(key, {})
            merged[key] = {**section_parent, **section_child}
        elif key in child:
            merged[key] = child[key]
        else:
            merged[key] = parent[key]
    return merged


def _apply_removes(payload: CatalogRaw, removes: tuple[str, ...]) -> CatalogRaw:
    """Delete entries named by dotted paths (e.g. ``"storage.sqlite"``)."""
    if not removes:
        return payload
    import logging

    log = logging.getLogger("skaal.catalog")
    out: CatalogRaw = {
        section: dict(values) if isinstance(values, dict) else values
        for section, values in payload.items()
    }
    for dotted in removes:
        section, _, name = dotted.partition(".")
        if not section or not name:
            raise CatalogError(
                f"[{_RESERVED_TABLE}] remove entries must be dotted "
                f"'section.name' paths; got {dotted!r}."
            )
        bucket = out.get(section)
        if not isinstance(bucket, dict) or name not in bucket:
            log.warning("catalog remove %r: nothing to remove", dotted)
            continue
        del bucket[name]
    return out


def _flatten_chain(source: CatalogSource) -> CatalogRaw:
    """Walk the chain root → leaf, merging payloads and applying removes."""
    merged: CatalogRaw = {}
    for node in source.chain():
        merged = _merge_payloads(merged, node.raw)
        merged = _apply_removes(merged, node.removes)
    return merged


def load_catalog(path: Path | str | None = None, target: str | None = None) -> dict[str, Any]:
    """Load a catalog TOML and return the merged raw dict.

    Any ``[skaal] extends`` chain is flattened and ``[skaal] remove`` entries
    are pruned.  The reserved ``[skaal]`` table itself is stripped from the
    result so downstream consumers (Pydantic models, the solver, etc.) do
    not need to know it exists.

    Args:
        path: Explicit path to a catalog file, or a short name registered via
              :func:`skaal.plugins.register_catalog` / ``skaal.catalogs``
              entry points (e.g. ``"aws"`` from an ``skaal-aws`` addon).
        target: Deploy target name (e.g., 'aws', 'gcp', 'aws-lambda') used to
                bias filesystem search when *path* is not given.
    """
    return _flatten_chain(load_catalog_with_sources(path, target=target))


def load_catalog_with_sources(
    path: Path | str | None = None, target: str | None = None
) -> CatalogSource:
    """Like :func:`load_catalog` but return the resolved :class:`CatalogSource`.

    Use this when you need to introspect *where* each layer came from — the
    ``skaal catalog sources`` command and tests both rely on it.
    """
    resolved = _resolve_path(path, target)
    if isinstance(resolved, dict):
        return _build_source_chain(resolved, path=None)
    return _build_source_chain(_read_toml(resolved), path=resolved)


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
