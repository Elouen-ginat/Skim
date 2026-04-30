"""Typed KV storage container: ``Store[T]``.

``Store`` is Skaal's single KV abstraction. It supports both explicit key-based
access and model-centric access with inferred primary keys.

Usage::

    from pydantic import BaseModel
    from skaal import App, Store

    class Address(BaseModel):
        street: str
        city: str

    class User(BaseModel):
        id: str
        name: str
        address: Address
        tags: list[str] = []

    app = App("users")

    @app.storage(read_latency="< 10ms", durability="persistent")
    class Users(Store[User]):
        pass

    user = User(id="u1", name="Alice", address=Address(street="1 Main", city="NYC"))

    await Users.set("u1", user)          # explicit key
    alice = await Users.get("u1")        # returns User instance
    all_users = await Users.list()        # list[tuple[str, User]]

    await Users.add(user)                 # key inferred from user.id
    await Users.all()                     # list[User]
"""

from __future__ import annotations

import base64
import json
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ClassVar,
    Generic,
    TypeVar,
    get_args,
    get_origin,
    overload,
)

if TYPE_CHECKING:
    from skaal.backends.base import StorageBackend

from skaal.sync import run as _sync_run
from skaal.types.storage import Page, SecondaryIndex

T = TypeVar("T")
_PAGE_DRAIN_LIMIT = 1000


def _is_pydantic(t: Any) -> bool:
    try:
        from pydantic import BaseModel

        return isinstance(t, type) and issubclass(t, BaseModel)
    except ImportError:
        return False


def _serialize(value: Any, value_type: type | None) -> Any:
    """Convert *value* to a backend-storable form."""
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
    """Reconstruct a typed value from the raw backend representation."""
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


def _primary_key_field(model: type) -> str:
    """Infer the primary key field name from a Pydantic model."""
    if not hasattr(model, "model_fields"):
        return "id"
    fields = list(model.model_fields.keys())
    for candidate in ("id", "pk", "key"):
        if candidate in fields:
            return candidate
    return fields[0] if fields else "id"


def _schema_hints(cls: type) -> dict[str, Any]:
    """
    Extract solver-visible hints from a ``Store`` subclass.

    These are stored in ``__skaal_storage__["schema"]`` and surfaced in
    ``skaal plan`` output. They do not yet change backend selection.
    """
    hints: dict[str, Any] = {}
    value_type = getattr(cls, "__skaal_value_type__", None)
    if not _is_pydantic(value_type) or value_type is None:
        return hints

    fields = value_type.model_fields
    nested_count = sum(1 for field in fields.values() if _is_pydantic(field.annotation))
    list_count = sum(
        1
        for field in fields.values()
        if field.annotation is not None and get_origin(field.annotation) is list
    )
    hints["model"] = value_type.__name__
    hints["field_count"] = len(fields)
    hints["nested_models"] = nested_count
    hints["list_fields"] = list_count
    hints["prefers_sql"] = nested_count > 0 or len(fields) > 10
    return hints


def _normalize_limit(limit: int) -> int:
    if limit <= 0:
        raise ValueError("limit must be > 0")
    return limit


def _cursor_identity(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None) -> dict[str, Any]:
    if cursor is None:
        return {}
    padded = cursor + "=" * (-len(cursor) % 4)
    raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    decoded = json.loads(raw.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("Invalid cursor")
    return decoded


def _sort_token(value: Any) -> tuple[int, Any]:
    if value is None:
        return (0, "")
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, (int, float)):
        return (2, value)
    if isinstance(value, str):
        return (3, value)
    return (4, json.dumps(value, sort_keys=True, default=str))


def _field_value(value: Any, field_name: str) -> Any:
    if isinstance(value, dict):
        return value.get(field_name)
    return getattr(value, field_name, None)


def _configure_backend_indexes(backend: Any, indexes: list[SecondaryIndex]) -> None:
    setattr(backend, "_skaal_secondary_indexes", {index.name: index for index in indexes})


def _get_backend_indexes(backend: Any) -> dict[str, SecondaryIndex]:
    indexes = getattr(backend, "_skaal_secondary_indexes", {})
    if isinstance(indexes, dict):
        normalized: dict[str, SecondaryIndex] = {}
        for name, value in indexes.items():
            if isinstance(value, SecondaryIndex):
                normalized[name] = value
                continue
            if isinstance(value, dict):
                normalized[name] = SecondaryIndex(name=name, **value)
        return normalized
    return {}


def _page_items(
    items: list[T],
    *,
    limit: int,
    cursor: str | None,
    mode: str,
    extra: dict[str, Any] | None = None,
) -> Page[T]:
    limit = _normalize_limit(limit)
    expected = {"mode": mode, **(extra or {})}
    decoded = _decode_cursor(cursor)
    for key, value in expected.items():
        if decoded and decoded.get(key) != value:
            raise ValueError("Cursor does not match this query")
    offset = int(decoded.get("offset", 0)) if decoded else 0
    if offset < 0:
        raise ValueError("Cursor offset must be >= 0")
    page_items = items[offset : offset + limit]
    next_offset = offset + len(page_items)
    has_more = next_offset < len(items)
    next_cursor = None
    if has_more:
        next_cursor = _encode_cursor({**expected, "offset": next_offset})
    return Page(items=page_items, next_cursor=next_cursor, has_more=has_more)


def _list_page_from_entries(
    entries: list[tuple[str, Any]],
    *,
    limit: int,
    cursor: str | None,
) -> Page[tuple[str, Any]]:
    ordered = sorted(entries, key=lambda item: item[0])
    return _page_items(ordered, limit=limit, cursor=cursor, mode="list")


def _scan_page_from_entries(
    entries: list[tuple[str, Any]],
    *,
    prefix: str,
    limit: int,
    cursor: str | None,
) -> Page[tuple[str, Any]]:
    ordered = sorted(entries, key=lambda item: item[0])
    return _page_items(
        ordered,
        limit=limit,
        cursor=cursor,
        mode="scan",
        extra={"prefix": prefix},
    )


def _query_index_from_entries(
    entries: list[tuple[str, Any]],
    *,
    backend: Any,
    index_name: str,
    key: Any,
    limit: int,
    cursor: str | None,
) -> Page[Any]:
    indexes = _get_backend_indexes(backend)
    index = indexes.get(index_name)
    if index is None:
        raise ValueError(f"No secondary index named {index_name!r}")

    matches: list[tuple[str, Any, tuple[int, Any]]] = []
    for primary_key, value in entries:
        if _field_value(value, index.partition_key) != key:
            continue
        sort_value = (
            _field_value(value, index.sort_key) if index.sort_key is not None else primary_key
        )
        matches.append((primary_key, value, _sort_token(sort_value)))

    matches.sort(key=lambda item: (item[2], item[0]))
    ordered_values = [value for _, value, _ in matches]
    return _page_items(
        ordered_values,
        limit=limit,
        cursor=cursor,
        mode="index",
        extra={"index_name": index_name, "key": _cursor_identity(key)},
    )


# Preserve the builtin ``list`` reference before ``Store`` defines a classmethod
# with the same name.
_List = list


class Store(Generic[T]):
    """
    Typed key-value storage with optional model-centric helpers.

    ``Store`` always uses string keys. If ``T`` is a Pydantic model, Skaal will
    validate dict inputs on write and return typed model instances on read.

    For model-centric usage, ``add()`` infers the key from ``id``, ``pk``,
    ``key``, or the model's first field. ``update()`` accepts either a full
    replacement value or an atomic transform function.
    """

    __skaal_key_type__: ClassVar[type] = str
    __skaal_value_type__: ClassVar[type | None] = None
    __skaal_key_field__: ClassVar[str] = "id"
    _backend: ClassVar[StorageBackend | None] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for base in getattr(cls, "__orig_bases__", []):
            origin = get_origin(base)
            if origin is Store or (isinstance(origin, type) and issubclass(origin, Store)):
                args = get_args(base)
                if args:
                    cls.__skaal_value_type__ = args[0]
                    if "__skaal_key_field__" not in cls.__dict__:
                        cls.__skaal_key_field__ = _primary_key_field(args[0])
                break

    @classmethod
    def wire(cls, backend: StorageBackend) -> None:
        """Bind *backend* to this storage class."""
        cls._backend = backend
        indexes = list(getattr(cls, "__skaal_storage__", {}).get("indexes", []))
        _configure_backend_indexes(backend, indexes)

    @classmethod
    def _ensure_wired(cls) -> None:
        if cls._backend is None:
            raise NotImplementedError(
                f"{cls.__name__} storage not wired. Use LocalRuntime or deploy first."
            )

    @classmethod
    def _extract_key(cls, item: T) -> str:
        """Extract the storage key from *item* using ``cls.__skaal_key_field__``."""
        key_field = cls.__skaal_key_field__
        if hasattr(item, key_field):
            return str(getattr(item, key_field))
        if isinstance(item, dict) and key_field in item:
            return str(item[key_field])
        raise ValueError(
            f"Cannot extract key field {key_field!r} from {type(item).__name__}. "
            f"Set __skaal_key_field__ on the storage class to override."
        )

    @classmethod
    async def get(cls, key: str) -> T | None:
        """Return the value for *key*, or ``None`` if not found."""
        cls._ensure_wired()
        assert cls._backend is not None
        raw = await cls._backend.get(key)
        return _deserialize(raw, cls.__skaal_value_type__)

    @classmethod
    async def set(cls, key: str, value: T) -> None:
        """Store *value* under *key*."""
        cls._ensure_wired()
        assert cls._backend is not None
        await cls._backend.set(key, _serialize(value, cls.__skaal_value_type__))

    @classmethod
    async def delete(cls, key: str) -> None:
        """Remove *key* (no-op if not present)."""
        cls._ensure_wired()
        assert cls._backend is not None
        await cls._backend.delete(key)

    @classmethod
    async def list(cls) -> _List[tuple[str, T]]:
        """Return all ``(key, value)`` pairs."""
        items: _List[tuple[str, T]] = []
        cursor: str | None = None
        while True:
            page = await cls.list_page(limit=_PAGE_DRAIN_LIMIT, cursor=cursor)
            items.extend(page.items)
            if not page.has_more:
                return items
            cursor = page.next_cursor

    @classmethod
    async def scan(cls, prefix: str = "") -> _List[tuple[str, T]]:
        """Return all ``(key, value)`` pairs where key starts with *prefix*."""
        items: _List[tuple[str, T]] = []
        cursor: str | None = None
        while True:
            page = await cls.scan_page(prefix=prefix, limit=_PAGE_DRAIN_LIMIT, cursor=cursor)
            items.extend(page.items)
            if not page.has_more:
                return items
            cursor = page.next_cursor

    @classmethod
    async def list_page(
        cls,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> Page[tuple[str, T]]:
        cls._ensure_wired()
        assert cls._backend is not None
        page = await cls._backend.list_page(limit=limit, cursor=cursor)
        return Page(
            items=[
                (key, _deserialize(value, cls.__skaal_value_type__)) for key, value in page.items
            ],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    @classmethod
    async def scan_page(
        cls,
        prefix: str = "",
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> Page[tuple[str, T]]:
        cls._ensure_wired()
        assert cls._backend is not None
        page = await cls._backend.scan_page(prefix=prefix, limit=limit, cursor=cursor)
        return Page(
            items=[
                (key, _deserialize(value, cls.__skaal_value_type__)) for key, value in page.items
            ],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    @classmethod
    async def query_index(
        cls,
        index_name: str,
        key: Any,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> Page[T]:
        cls._ensure_wired()
        assert cls._backend is not None
        page = await cls._backend.query_index(index_name, key, limit=limit, cursor=cursor)
        return Page(
            items=[_deserialize(value, cls.__skaal_value_type__) for value in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    @classmethod
    async def close(cls) -> None:
        """Release any resources held by the backend."""
        if cls._backend is not None:
            await cls._backend.close()

    @classmethod
    async def add(cls, item: T) -> None:
        """Store *item* using its inferred primary key."""
        await cls.set(cls._extract_key(item), item)

    @classmethod
    async def remove(cls, key: str) -> None:
        """Alias for :meth:`delete`."""
        await cls.delete(key)

    @classmethod
    async def all(cls) -> _List[T]:
        """Return all items as a flat list."""
        return [value for _, value in await cls.list()]

    @classmethod
    async def find(cls, prefix: str = "") -> _List[T]:
        """Return all values whose key starts with *prefix*."""
        return [value for _, value in await cls.scan(prefix)]

    @overload
    @classmethod
    async def update(cls, key: str, value_or_fn: Callable[[T | None], T]) -> T: ...

    @overload
    @classmethod
    async def update(cls, key: str, value_or_fn: T) -> T: ...

    @classmethod
    async def update(cls, key: str, value_or_fn: Callable[[T | None], T] | T) -> T:
        """
        Replace the value at *key* or atomically transform it.

        Pass a callable to perform a read-modify-write under the backend lock.
        Pass a concrete value to replace the existing entry directly.
        """
        if callable(value_or_fn):
            cls._ensure_wired()
            assert cls._backend is not None
            value_type = cls.__skaal_value_type__

            def _wrapped(raw: Any) -> Any:
                current = _deserialize(raw, value_type)
                updated = value_or_fn(current)
                return _serialize(updated, value_type)

            raw_result = await cls._backend.atomic_update(key, _wrapped)
            return _deserialize(raw_result, cls.__skaal_value_type__)

        payload = _serialize(value_or_fn, cls.__skaal_value_type__)
        await cls.set(key, value_or_fn)
        return _deserialize(payload, cls.__skaal_value_type__)

    @classmethod
    def sync_get(cls, key: str) -> T | None:
        """Synchronous wrapper for :meth:`get`."""
        return _sync_run(cls.get(key))

    @classmethod
    def sync_set(cls, key: str, value: T) -> None:
        """Synchronous wrapper for :meth:`set`."""
        _sync_run(cls.set(key, value))

    @classmethod
    def sync_delete(cls, key: str) -> None:
        """Synchronous wrapper for :meth:`delete`."""
        _sync_run(cls.delete(key))

    @classmethod
    def sync_list(cls) -> _List[tuple[str, T]]:
        """Synchronous wrapper for :meth:`list`."""
        return _sync_run(cls.list())

    @classmethod
    def sync_list_page(
        cls,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> Page[tuple[str, T]]:
        """Synchronous wrapper for :meth:`list_page`."""
        return _sync_run(cls.list_page(limit=limit, cursor=cursor))

    @classmethod
    def sync_scan(cls, prefix: str = "") -> _List[tuple[str, T]]:
        """Synchronous wrapper for :meth:`scan`."""
        return _sync_run(cls.scan(prefix))

    @classmethod
    def sync_scan_page(
        cls,
        prefix: str = "",
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> Page[tuple[str, T]]:
        """Synchronous wrapper for :meth:`scan_page`."""
        return _sync_run(cls.scan_page(prefix=prefix, limit=limit, cursor=cursor))

    @classmethod
    def sync_query_index(
        cls,
        index_name: str,
        key: Any,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> Page[T]:
        """Synchronous wrapper for :meth:`query_index`."""
        return _sync_run(cls.query_index(index_name, key, limit=limit, cursor=cursor))

    @classmethod
    def sync_add(cls, item: T) -> None:
        """Synchronous wrapper for :meth:`add`."""
        _sync_run(cls.add(item))

    @classmethod
    def sync_all(cls) -> _List[T]:
        """Synchronous wrapper for :meth:`all`."""
        return _sync_run(cls.all())

    @classmethod
    def sync_find(cls, prefix: str = "") -> _List[T]:
        """Synchronous wrapper for :meth:`find`."""
        return _sync_run(cls.find(prefix))

    @classmethod
    def sync_update(cls, key: str, value_or_fn: Callable[[T | None], T] | T) -> T:
        """Synchronous wrapper for :meth:`update`."""
        return _sync_run(cls.update(key, value_or_fn))
