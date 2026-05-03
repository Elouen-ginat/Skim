"""Plugin discovery for backends, channels, and named catalogs.

Skaal keeps a small built-in backend map for first-party implementations and
uses standard Python entry points for third-party extensions:

* ``skaal.backends``  — async key-value storage backends
* ``skaal.channels``  — channel wiring functions
* ``skaal.catalogs``  — named catalog TOML files resolvable by short name
"""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Callable

from skaal.errors import SkaalPluginError

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


def get_backend(name: str) -> Any:
    builtins = _load_builtin("skaal.backends")
    if name in builtins:
        return builtins[name]
    discovered = _load_group("skaal.backends")
    if name in discovered:
        return discovered[name]
    available = sorted(set(builtins) | set(discovered))
    raise SkaalPluginError(
        f"Unknown storage backend {name!r}. Registered: {available or '(none)'}."
    )


def iter_backends() -> dict[str, Any]:
    """Return every built-in and entry-point backend name→factory."""
    return {**_load_group("skaal.backends"), **_load_builtin("skaal.backends")}


# ── Channels ──────────────────────────────────────────────────────────────────


def get_channel(name: str) -> Callable[..., None]:
    """Return the ``wire_<name>`` function for *name*."""
    discovered = _load_group("skaal.channels")
    if name in discovered:
        return discovered[name]
    available = sorted(discovered)
    raise SkaalPluginError(
        f"Unknown channel backend {name!r}. Registered: {available or '(none)'}."
    )


def iter_channels() -> dict[str, Callable[..., None]]:
    return _load_group("skaal.channels")


# ── Named catalogs ────────────────────────────────────────────────────────────


def get_catalog_path(name: str) -> Path:
    """Resolve a short catalog name (e.g. ``"aws"``) to a file path.

    Entry-point handlers may be either a :class:`pathlib.Path`, a ``str``,
    or a zero-arg callable that returns one — the last form lets a package
    compute the path dynamically (e.g. from ``importlib.resources``).
    """
    discovered = _load_group("skaal.catalogs")
    if name in discovered:
        value = discovered[name]
        if callable(value):
            value = value()
        return Path(value)
    available = sorted(discovered)
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
    return merged


__all__ = [
    "clear_cache",
    "get_backend",
    "get_catalog_path",
    "get_channel",
    "iter_backends",
    "iter_catalogs",
    "iter_channels",
]
