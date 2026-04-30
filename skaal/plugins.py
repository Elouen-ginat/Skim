"""Plugin discovery for backends, channels, and named catalogs.

Skaal resolves concrete implementations through three extension points that
any third-party package can register without modifying core:

* ``skaal.backends``  — async key-value storage backends (``StorageBackend`` protocol)
* ``skaal.channels``  — channel wiring functions (``wire_<name>(channel, **kwargs)``)
* ``skaal.catalogs``  — named catalog TOML files resolvable by short name

Entry-point registration (in a distributed package's ``pyproject.toml``)::

    [project.entry-points."skaal.backends"]
    azure_tables = "skaal_azure.tables:AzureTablesBackend"

    [project.entry-points."skaal.channels"]
    kafka = "skaal_kafka:wire_kafka"

    [project.entry-points."skaal.catalogs"]
    azure = "skaal_azure:catalog_path"

In-process registration is also supported (useful for tests and notebooks)::

    from skaal.plugins import register_backend
    register_backend("mycache", MyCacheBackend)
"""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Callable

from skaal.errors import SkaalPluginError

# ── In-process registries (take precedence over entry_points) ─────────────────

_backends: dict[str, Any] = {}
_channels: dict[str, Callable[..., None]] = {}
_catalogs: dict[str, Path] = {}

# ── Cache of entry-point results (populated lazily, flushable for tests) ──────

_ep_cache: dict[str, dict[str, Any]] = {}

_BUILTIN_BACKENDS: dict[str, str] = {
    "local": "skaal.backends.local_backend:LocalMap",
    "local-blob": "skaal.backends.file_blob_backend:FileBlobBackend",
    "sqlite": "skaal.backends.sqlite_backend:SqliteBackend",
    "redis": "skaal.backends.redis_backend:RedisBackend",
    "postgres": "skaal.backends.postgres_backend:PostgresBackend",
    "chroma": "skaal.backends.chroma_backend:ChromaVectorBackend",
    "pgvector": "skaal.backends.pgvector_backend:PgVectorBackend",
    "dynamodb": "skaal.backends.dynamodb_backend:DynamoBackend",
    "firestore": "skaal.backends.firestore_backend:FirestoreBackend",
    "s3": "skaal.backends.s3_blob_backend:S3BlobBackend",
    "gcs": "skaal.backends.gcs_blob_backend:GCSBlobBackend",
}


def _load_builtin(group: str) -> dict[str, Any]:
    if group != "skaal.backends":
        return {}
    resolved: dict[str, Any] = {}
    for name, target in _BUILTIN_BACKENDS.items():
        module_name, _, attr_name = target.partition(":")
        module = import_module(module_name)
        resolved[name] = getattr(module, attr_name)
    return resolved


def _load_group(group: str) -> dict[str, Any]:
    """Return a name→object mapping for every entry point in *group*.

    Cached; use :func:`clear_cache` to force re-discovery after installing
    a new plugin in the same process.
    """
    if group in _ep_cache:
        return _ep_cache[group]
    discovered: dict[str, Any] = {}
    try:
        eps = entry_points(group=group)
    except TypeError:  # pragma: no cover — very old importlib.metadata
        eps = entry_points().get(group, [])  # type: ignore[arg-type]
    for ep in eps:
        try:
            discovered[ep.name] = ep.load()
        except Exception as exc:  # noqa: BLE001
            raise SkaalPluginError(
                f"Failed to load entry point {ep.name!r} in group {group!r}: {exc}"
            ) from exc
    _ep_cache[group] = discovered
    return discovered


def clear_cache() -> None:
    """Drop cached entry-point lookups — tests can re-read after installs."""
    _ep_cache.clear()


# ── Backends ──────────────────────────────────────────────────────────────────


def register_backend(name: str, factory: Any) -> None:
    """Register a storage backend under *name* (in-process; no pyproject edit)."""
    _backends[name] = factory


def get_backend(name: str) -> Any:
    """Return the factory/class registered for *name*.

    In-process registrations beat entry points so tests can override installed
    plugins.  Raises :class:`SkaalPluginError` if nothing matches.
    """
    if name in _backends:
        return _backends[name]
    discovered = _load_group("skaal.backends")
    if name in discovered:
        return discovered[name]
    builtins = _load_builtin("skaal.backends")
    if name in builtins:
        return builtins[name]
    available = sorted(set(_backends) | set(discovered))
    raise SkaalPluginError(
        f"Unknown storage backend {name!r}. Registered: {available or '(none)'}."
    )


def iter_backends() -> dict[str, Any]:
    """Return every registered backend name→factory, entry points + in-process."""
    merged: dict[str, Any] = {}
    merged.update(_load_group("skaal.backends"))
    merged.update(_load_builtin("skaal.backends"))
    merged.update(_backends)  # in-process overrides
    return merged


# ── Channels ──────────────────────────────────────────────────────────────────


def register_channel(name: str, wire_fn: Callable[..., None]) -> None:
    """Register a channel-wiring function under *name*."""
    _channels[name] = wire_fn


def get_channel(name: str) -> Callable[..., None]:
    """Return the ``wire_<name>`` function for *name*."""
    if name in _channels:
        return _channels[name]
    discovered = _load_group("skaal.channels")
    if name in discovered:
        return discovered[name]
    available = sorted(set(_channels) | set(discovered))
    raise SkaalPluginError(
        f"Unknown channel backend {name!r}. Registered: {available or '(none)'}."
    )


def iter_channels() -> dict[str, Callable[..., None]]:
    merged: dict[str, Callable[..., None]] = {}
    merged.update(_load_group("skaal.channels"))
    merged.update(_channels)
    return merged


# ── Named catalogs ────────────────────────────────────────────────────────────


def register_catalog(name: str, path: Path | str) -> None:
    """Register a named catalog TOML under *name*.

    Plain filesystem paths still work — this is for addons that ship a
    catalog TOML inside their package.
    """
    _catalogs[name] = Path(path)


def get_catalog_path(name: str) -> Path:
    """Resolve a short catalog name (e.g. ``"aws"``) to a file path.

    Entry-point handlers may be either a :class:`pathlib.Path`, a ``str``,
    or a zero-arg callable that returns one — the last form lets a package
    compute the path dynamically (e.g. from ``importlib.resources``).
    """
    if name in _catalogs:
        return _catalogs[name]
    discovered = _load_group("skaal.catalogs")
    if name in discovered:
        value = discovered[name]
        if callable(value):
            value = value()
        return Path(value)
    available = sorted(set(_catalogs) | set(discovered))
    raise SkaalPluginError(f"Unknown catalog name {name!r}. Registered: {available or '(none)'}.")


def iter_catalogs() -> dict[str, Path]:
    merged: dict[str, Path] = {}
    for n, v in _load_group("skaal.catalogs").items():
        if callable(v):
            try:
                v = v()
            except Exception:  # noqa: BLE001
                continue
        merged[n] = Path(v)
    merged.update(_catalogs)
    return merged


__all__ = [
    "clear_cache",
    "get_backend",
    "get_catalog_path",
    "get_channel",
    "iter_backends",
    "iter_catalogs",
    "iter_channels",
    "register_backend",
    "register_catalog",
    "register_channel",
]
