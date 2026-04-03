"""In-memory storage backend and schema-aware class patching utilities."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
from typing import Any


# ── Sync/async bridge ─────────────────────────────────────────────────────────

# A module-level thread executor used by the sync wrappers to safely run async
# operations from synchronous contexts (Dash callbacks, Flask views, Django
# views, etc.) even when an event loop is already running in the current thread.
_sync_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="skaal-sync",
)


def _sync_run(coro: Any) -> Any:
    """
    Run *coro* from a synchronous context.

    Handles both cases:

    - **No running event loop** (typical in sync frameworks like Dash/Flask):
      uses ``asyncio.run()``.
    - **Running event loop in current thread** (e.g., called from inside
      uvicorn/asyncio code): submits to a separate thread with its own loop,
      blocking until done.  This avoids the "cannot run nested event loop"
      error.

    Example (Dash callback)::

        @callback(Output("graph", "figure"), Input("session-id", "data"))
        def update_graph(session_id):
            state = Sessions.sync_get(session_id)  # safe in Dash callbacks
            ...
    """
    try:
        asyncio.get_running_loop()
        # A loop is running in this thread (e.g., uvicorn).
        # Off-load to a dedicated thread that can create its own loop.
        return _sync_executor.submit(asyncio.run, coro).result()
    except RuntimeError:
        # No running loop — create one inline.
        return asyncio.run(coro)


# ── Serialization helpers ──────────────────────────────────────────────────────

def _serialize(value: Any, value_type: type | None) -> Any:
    """
    Convert *value* to a backend-storable form.

    - Pydantic model instance → ``dict`` (via ``model_dump()``)
    - Plain ``dict`` when *value_type* is a Pydantic model → validated first,
      then converted to ``dict`` (catches bad data early)
    - Anything else → returned unchanged
    """
    if value_type is None:
        return value
    try:
        from pydantic import BaseModel
        if isinstance(value_type, type) and issubclass(value_type, BaseModel):
            if isinstance(value, BaseModel):
                return value.model_dump()
            if isinstance(value, dict):
                return value_type.model_validate(value).model_dump()
    except ImportError:
        pass
    return value


def _deserialize(raw: Any, value_type: type | None) -> Any:
    """
    Reconstruct a typed value from the raw backend representation.

    - ``dict`` + Pydantic *value_type* → run ``apply_migrations()`` then
      ``value_type.model_validate(migrated)``
    - JSON ``str``/``bytes`` + Pydantic *value_type* → parse to ``dict`` first
    - Already the right type → returned as-is
    - ``None`` → ``None``
    - Anything else → returned unchanged
    """
    if raw is None or value_type is None:
        return raw
    try:
        from pydantic import BaseModel
        if isinstance(value_type, type) and issubclass(value_type, BaseModel):
            if isinstance(raw, value_type):
                return raw
            if isinstance(raw, (str, bytes)):
                raw = json.loads(raw)
            if isinstance(raw, dict):
                from skaal.types.schema import apply_migrations
                return value_type.model_validate(apply_migrations(raw, value_type))
    except ImportError:
        pass
    return raw


# ── LocalMap ───────────────────────────────────────────────────────────────────

class LocalMap:
    """
    In-memory key-value store that satisfies the :class:`~skaal.backends.base.StorageBackend`
    protocol.

    Used by :class:`~skaal.runtime.local.LocalRuntime` to back storage classes
    during local development and testing.  All methods are async to match the
    production backend interface.
    """

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def get(self, key: str) -> Any | None:
        return self._data.get(key)

    async def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def list(self) -> list[tuple[str, Any]]:
        return list(self._data.items())

    async def scan(self, prefix: str = "") -> list[tuple[str, Any]]:
        return [(k, v) for k, v in self._data.items() if k.startswith(prefix)]

    async def close(self) -> None:
        pass

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"LocalMap({len(self._data)} keys)"


# ── patch_storage_class ────────────────────────────────────────────────────────

def patch_storage_class(cls: type, backend: Any) -> None:
    """
    Inject *backend* as class-level async methods on *cls*.

    Plain storage classes (``class Counts: pass``) get raw get/set/delete/
    list/scan/close with zero overhead.

    Typed storage classes (``class Profiles(Map[str, User])`` or
    ``class Profiles(Collection[User])``) additionally get:

    - Automatic Pydantic validation and schema migration on read/write
    - ``Collection`` subclasses also get ``add()``, ``remove()``,
      ``update()``, ``all()``, ``find()``

    The backend is also available as ``cls._backend`` for direct access.
    """
    from skaal.storage import Collection, Map

    value_type: type | None = getattr(cls, "__skaal_value_type__", None)
    is_map = isinstance(cls, type) and issubclass(cls, Map)
    is_collection = isinstance(cls, type) and issubclass(cls, Collection)
    use_schema = (is_map or is_collection) and value_type is not None

    cls._backend = backend  # type: ignore[attr-defined]

    async def _get(key: str) -> Any | None:
        raw = await backend.get(key)
        return _deserialize(raw, value_type) if use_schema else raw

    async def _set(key: str, value: Any) -> None:
        payload = _serialize(value, value_type) if use_schema else value
        await backend.set(key, payload)

    async def _delete(key: str) -> None:
        await backend.delete(key)

    async def _list() -> list[tuple[str, Any]]:
        entries = await backend.list()
        if use_schema:
            return [(k, _deserialize(v, value_type)) for k, v in entries]
        return entries

    async def _scan(prefix: str = "") -> list[tuple[str, Any]]:
        entries = await backend.scan(prefix)
        if use_schema:
            return [(k, _deserialize(v, value_type)) for k, v in entries]
        return entries

    async def _close() -> None:
        await backend.close()

    # ── Sync wrappers — safe to call from Dash callbacks, Flask views, etc. ──

    def _sync_get(key: str) -> Any | None:
        return _sync_run(_get(key))

    def _sync_set(key: str, value: Any) -> None:
        _sync_run(_set(key, value))

    def _sync_delete(key: str) -> None:
        _sync_run(_delete(key))

    def _sync_list() -> list[tuple[str, Any]]:
        return _sync_run(_list())

    def _sync_scan(prefix: str = "") -> list[tuple[str, Any]]:
        return _sync_run(_scan(prefix))

    cls.get = staticmethod(_get)               # type: ignore[attr-defined]
    cls.set = staticmethod(_set)               # type: ignore[attr-defined]
    cls.delete = staticmethod(_delete)         # type: ignore[attr-defined]
    cls.list = staticmethod(_list)             # type: ignore[attr-defined]
    cls.scan = staticmethod(_scan)             # type: ignore[attr-defined]
    cls.close = staticmethod(_close)           # type: ignore[attr-defined]
    cls.sync_get = staticmethod(_sync_get)     # type: ignore[attr-defined]
    cls.sync_set = staticmethod(_sync_set)     # type: ignore[attr-defined]
    cls.sync_delete = staticmethod(_sync_delete) # type: ignore[attr-defined]
    cls.sync_list = staticmethod(_sync_list)   # type: ignore[attr-defined]
    cls.sync_scan = staticmethod(_sync_scan)   # type: ignore[attr-defined]

    if is_collection:
        key_field: str = getattr(cls, "__skaal_key_field__", "id")

        def _extract_key(item: Any) -> str:
            if hasattr(item, key_field):
                return str(getattr(item, key_field))
            if isinstance(item, dict) and key_field in item:
                return str(item[key_field])
            raise ValueError(
                f"Cannot extract key field {key_field!r} from {type(item).__name__}. "
                f"Set __skaal_key_field__ on the storage class to override."
            )

        async def _add(item: Any) -> None:
            await _set(_extract_key(item), item)

        async def _remove(key: str) -> None:
            await backend.delete(key)

        async def _update(key: str, item: Any) -> None:
            await _set(key, item)

        async def _all() -> list[Any]:
            return [v for _, v in await _list()]

        async def _find(prefix: str = "") -> list[Any]:
            return [v for _, v in await _scan(prefix)]

        def _sync_add(item: Any) -> None:
            _sync_run(_add(item))

        def _sync_all() -> list[Any]:
            return _sync_run(_all())

        def _sync_find(prefix: str = "") -> list[Any]:
            return _sync_run(_find(prefix))

        cls.add = staticmethod(_add)               # type: ignore[attr-defined]
        cls.remove = staticmethod(_remove)         # type: ignore[attr-defined]
        cls.update = staticmethod(_update)         # type: ignore[attr-defined]
        cls.all = staticmethod(_all)               # type: ignore[attr-defined]
        cls.find = staticmethod(_find)             # type: ignore[attr-defined]
        cls.sync_add = staticmethod(_sync_add)     # type: ignore[attr-defined]
        cls.sync_all = staticmethod(_sync_all)     # type: ignore[attr-defined]
        cls.sync_find = staticmethod(_sync_find)   # type: ignore[attr-defined]
