"""In-memory storage backends and schema-aware class patching."""

from __future__ import annotations

from typing import Any


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
    Reconstruct a typed value from what the backend returned.

    - ``dict`` + Pydantic value_type → run ``apply_migrations()`` then
      ``value_type.model_validate(raw)``
    - JSON string + Pydantic value_type → parse to dict first, then same path
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
                import json as _json
                raw = _json.loads(raw)
            if isinstance(raw, dict):
                from skaal.types.schema import apply_migrations
                migrated = apply_migrations(raw, value_type)
                return value_type.model_validate(migrated)
    except ImportError:
        pass
    return raw


# ── LocalMap ──────────────────────────────────────────────────────────────────

class LocalMap:
    """
    In-memory key-value store.

    Patched onto storage classes during ``LocalRuntime`` startup so that
    user code can call ``await MyStorage.get(key)`` without any real backend.

    All methods are async to match the production backend interface.
    Values are stored as raw Python objects; serialization happens in the
    schema-aware patch layer, not here.
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
        """No-op for in-memory backend; satisfies the StorageBackend protocol."""

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"LocalMap({len(self._data)} keys)"


# ── patch_storage_class ────────────────────────────────────────────────────────

def patch_storage_class(cls: type, backend: Any) -> None:
    """
    Inject *backend* as class-level async methods on *cls*.

    **Plain storage classes** (``class Counts: pass``) get raw get/set/delete/
    list/scan/close — identical to before, zero overhead.

    **Typed storage classes** (``class Profiles(Map[str, User])`` or
    ``class Profiles(Collection[User])``) additionally get:

    - Automatic Pydantic validation on ``set()``
    - Automatic deserialization on ``get()`` / ``list()`` / ``scan()``
    - ``Collection`` subclasses also get ``add()``, ``remove()``,
      ``update()``, ``all()``, ``find()``

    The backend is also available as ``cls._local`` for direct access.
    """
    from skaal.storage import Collection, Map

    value_type: type | None = getattr(cls, "__skaal_value_type__", None)
    is_map = isinstance(cls, type) and issubclass(cls, Map)
    is_collection = isinstance(cls, type) and issubclass(cls, Collection)
    use_schema = (is_map or is_collection) and value_type is not None

    cls._local = backend  # type: ignore[attr-defined]

    # ── Core KV methods ──────────────────────────────────────────────────────

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

    cls.get = staticmethod(_get)          # type: ignore[attr-defined]
    cls.set = staticmethod(_set)          # type: ignore[attr-defined]
    cls.delete = staticmethod(_delete)    # type: ignore[attr-defined]
    cls.list = staticmethod(_list)        # type: ignore[attr-defined]
    cls.scan = staticmethod(_scan)        # type: ignore[attr-defined]
    cls.close = staticmethod(_close)      # type: ignore[attr-defined]

    # ── Collection convenience methods ───────────────────────────────────────

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

        cls.add = staticmethod(_add)      # type: ignore[attr-defined]
        cls.remove = staticmethod(_remove)  # type: ignore[attr-defined]
        cls.update = staticmethod(_update)  # type: ignore[attr-defined]
        cls.all = staticmethod(_all)      # type: ignore[attr-defined]
        cls.find = staticmethod(_find)    # type: ignore[attr-defined]
