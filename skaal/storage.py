"""Typed storage containers: Map[K, V] and Collection[T].

These are the recommended way to declare structured storage in Skaal.
They pair with any backend (LocalMap, Redis, SQLite, Postgres, DynamoDB)
and provide automatic Pydantic validation on write and deserialization on read.

Usage::

    from pydantic import BaseModel
    from skaal import App, Map, Collection

    class Address(BaseModel):
        street: str
        city: str

    class User(BaseModel):
        id: str
        name: str
        address: Address
        tags: list[str] = []

    app = App("users")

    # Explicit key-value: you provide the key
    @app.storage(read_latency="< 10ms", durability="persistent")
    class Users(Map[str, User]):
        pass

    # Model-centric: key is extracted from user.id automatically
    @app.storage(read_latency="< 10ms", durability="persistent")
    class UserStore(Collection[User]):
        pass

    # In a function:
    user = User(id="u1", name="Alice", address=Address(street="1 Main", city="NYC"))

    await Users.set("u1", user)           # stores serialized User
    alice = await Users.get("u1")         # returns User instance
    all_users = await Users.list()        # list[tuple[str, User]]

    await UserStore.add(user)             # key inferred from user.id
    await UserStore.all()                 # list[User]
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Generic, TypeVar, get_args, get_origin

if TYPE_CHECKING:
    from skaal.backends.base import StorageBackend

K = TypeVar("K")
V = TypeVar("V")
T = TypeVar("T")


# ── Serialization helpers ────────────────────────────────────────────────────
# These are used by Map/Collection classmethods and by patch_storage_class
# (backward-compat path for plain classes).


def _is_pydantic(t: Any) -> bool:
    try:
        from pydantic import BaseModel

        return isinstance(t, type) and issubclass(t, BaseModel)
    except ImportError:
        return False


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
    Extract solver-visible hints from a Map or Collection subclass.

    These are stored in ``__skaal_storage__["schema_hints"]`` and surfaced
    in ``skaal plan`` output.  They do not yet change backend selection —
    that is reserved for a future solver pass.
    """
    hints: dict[str, Any] = {}
    value_type = getattr(cls, "__skaal_value_type__", None)
    if not _is_pydantic(value_type) or value_type is None:
        return hints

    fields = value_type.model_fields
    nested_count = sum(1 for f in fields.values() if _is_pydantic(f.annotation))
    list_count = sum(
        1 for f in fields.values() if f.annotation is not None and get_origin(f.annotation) is list
    )
    hints["model"] = value_type.__name__
    hints["field_count"] = len(fields)
    hints["nested_models"] = nested_count
    hints["list_fields"] = list_count
    # Hint: deeply nested / wide schemas fit better in SQL/JSONB backends
    hints["prefers_sql"] = nested_count > 0 or len(fields) > 10
    return hints


# ── Map ──────────────────────────────────────────────────────────────────────


# Preserve the builtin `list` reference before Map/Collection define a
# classmethod also called `list`.  mypy resolves annotations inside those
# classes using the class namespace, which shadows the builtin.
_List = list


class Map(Generic[K, V]):
    """
    Typed key-value storage.

    ``K`` is the key type (informational — all backends use string keys).
    ``V`` is the value type:

    - If ``V`` is a Pydantic ``BaseModel``:
      - ``set()`` accepts a ``V`` instance *or* a plain ``dict`` (coerced via
        ``V.model_validate()``).
      - ``get()`` returns a ``V`` instance (or ``None``).
      - ``list()`` / ``scan()`` return ``list[tuple[str, V]]``.

    - Otherwise, ``V`` is stored and returned as-is (any JSON-serialisable type).

    Subclass inside ``@app.storage()``::

        @app.storage(read_latency="< 5ms", durability="persistent")
        class Sessions(Map[str, SessionToken]):
            pass

    The runtime sets ``cls._backend`` to a
    :class:`~skaal.backends.base.StorageBackend` instance.  All methods
    delegate to it — no monkey-patching needed.
    """

    __skaal_key_type__: ClassVar[type] = str
    __skaal_value_type__: ClassVar[type | None] = None
    _backend: ClassVar[StorageBackend | None] = None  # set per-subclass by the runtime

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for base in getattr(cls, "__orig_bases__", []):
            origin = get_origin(base)
            if origin is Map or (isinstance(origin, type) and issubclass(origin, Map)):
                args = get_args(base)
                if len(args) >= 1:
                    cls.__skaal_key_type__ = args[0]
                if len(args) >= 2:
                    cls.__skaal_value_type__ = args[1]
                break

    @classmethod
    def wire(cls, backend: StorageBackend) -> None:
        """Bind *backend* to this storage class. Called by :class:`~skaal.runtime.local.LocalRuntime`."""
        cls._backend = backend

    @classmethod
    def _ensure_wired(cls) -> None:
        if cls._backend is None:
            raise NotImplementedError(
                f"{cls.__name__} storage not wired. Use LocalRuntime or deploy first."
            )

    # ── Async API ─────────────────────────────────────────────────────────

    @classmethod
    async def get(cls, key: str) -> V | None:
        """Return the value for *key*, or ``None`` if not found."""
        cls._ensure_wired()
        assert cls._backend is not None
        raw = await cls._backend.get(key)
        if cls.__skaal_value_type__ is not None:
            return _deserialize(raw, cls.__skaal_value_type__)
        return raw

    @classmethod
    async def set(cls, key: str, value: V) -> None:
        """Store *value* under *key*."""
        cls._ensure_wired()
        assert cls._backend is not None
        payload = (
            _serialize(value, cls.__skaal_value_type__)
            if cls.__skaal_value_type__ is not None
            else value
        )
        await cls._backend.set(key, payload)

    @classmethod
    async def delete(cls, key: str) -> None:
        """Remove *key* (no-op if not present)."""
        cls._ensure_wired()
        assert cls._backend is not None
        await cls._backend.delete(key)

    @classmethod
    async def list(cls) -> _List[tuple[str, V]]:
        """Return all ``(key, value)`` pairs."""
        cls._ensure_wired()
        assert cls._backend is not None
        entries = await cls._backend.list()
        if cls.__skaal_value_type__ is not None:
            return [(k, _deserialize(v, cls.__skaal_value_type__)) for k, v in entries]
        return entries

    @classmethod
    async def scan(cls, prefix: str = "") -> _List[tuple[str, V]]:
        """Return all ``(key, value)`` pairs where key starts with *prefix*."""
        cls._ensure_wired()
        assert cls._backend is not None
        entries = await cls._backend.scan(prefix)
        if cls.__skaal_value_type__ is not None:
            return [(k, _deserialize(v, cls.__skaal_value_type__)) for k, v in entries]
        return entries

    @classmethod
    async def close(cls) -> None:
        """Release any resources held by the backend."""
        if cls._backend is not None:
            await cls._backend.close()

    # ── Sync API (safe in Dash / Flask callbacks) ─────────────────────────

    @classmethod
    def sync_get(cls, key: str) -> V | None:
        """Synchronous wrapper for :meth:`get`."""
        from skaal.backends.local_backend import _sync_run

        return _sync_run(cls.get(key))

    @classmethod
    def sync_set(cls, key: str, value: V) -> None:
        """Synchronous wrapper for :meth:`set`."""
        from skaal.backends.local_backend import _sync_run

        _sync_run(cls.set(key, value))

    @classmethod
    def sync_delete(cls, key: str) -> None:
        """Synchronous wrapper for :meth:`delete`."""
        from skaal.backends.local_backend import _sync_run

        _sync_run(cls.delete(key))

    @classmethod
    def sync_list(cls) -> _List[tuple[str, V]]:
        """Synchronous wrapper for :meth:`list`."""
        from skaal.backends.local_backend import _sync_run

        return _sync_run(cls.list())

    @classmethod
    def sync_scan(cls, prefix: str = "") -> _List[tuple[str, V]]:
        """Synchronous wrapper for :meth:`scan`."""
        from skaal.backends.local_backend import _sync_run

        return _sync_run(cls.scan(prefix))

    @classmethod
    async def update(cls, key: str, fn: Callable[[V | None], V]) -> V:
        """
        Atomically read, transform, and write back the value at *key*.

        *fn* receives the current value (deserialized, or ``None`` if absent)
        and must return the new value.  The read and write are performed inside
        the backend's lock so concurrent callbacks cannot interleave.

        Returns the new deserialized value.

        Example::

            def increment(state):
                if state is None:
                    state = UserState(session_id=sid)
                state.click_count += 1
                return state

            new_state = await Sessions.update(sid, increment)
        """
        cls._ensure_wired()
        assert cls._backend is not None
        value_type = cls.__skaal_value_type__

        def _wrapped(raw: Any) -> Any:
            current = _deserialize(raw, value_type)
            updated = fn(current)
            return _serialize(updated, value_type)

        raw_result = await cls._backend.atomic_update(key, _wrapped)
        return _deserialize(raw_result, value_type)

    @classmethod
    def sync_update(cls, key: str, fn: Callable[[V | None], V]) -> V:
        """Synchronous wrapper for :meth:`update`. Safe in Dash / Flask callbacks."""
        from skaal.backends.local_backend import _sync_run

        return _sync_run(cls.update(key, fn))


# ── Collection ───────────────────────────────────────────────────────────────


class Collection(Generic[T]):
    """
    Typed collection of Pydantic models with auto-extracted primary keys.

    The primary key field is resolved in this order:

    1. ``cls.__skaal_key_field__`` if explicitly set on the subclass.
    2. A field named ``"id"``, ``"pk"``, or ``"key"`` in ``T.model_fields``.
    3. The first field in ``T.model_fields``.

    In addition to the standard ``get / set / delete / list / scan`` methods
    (shared with ``Map``), ``Collection`` adds:

    - ``add(item)``      — store using the auto-extracted key.
    - ``remove(key)``    — alias for ``delete``.
    - ``update(key, item)`` — replace the value at ``key``.
    - ``all()``          — return all items as ``list[T]``.
    - ``find(prefix)``   — scan by key prefix, return ``list[T]``.

    Usage::

        @app.storage(durability="persistent")
        class Products(Collection[Product]):
            pass

        await Products.add(Product(id="p1", name="Widget"))
        widget = await Products.get("p1")     # Product instance
        all_products = await Products.all()   # list[Product]
    """

    __skaal_value_type__: ClassVar[type | None] = None
    __skaal_key_field__: ClassVar[str] = "id"
    _backend: ClassVar[StorageBackend | None] = None  # set per-subclass by the runtime

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for base in getattr(cls, "__orig_bases__", []):
            origin = get_origin(base)
            if origin is Collection or (
                isinstance(origin, type) and issubclass(origin, Collection)
            ):
                args = get_args(base)
                if args:
                    cls.__skaal_value_type__ = args[0]
                    # Only infer key field if the subclass hasn't overridden it
                    if not cls.__dict__.get("__skaal_key_field__"):
                        cls.__skaal_key_field__ = _primary_key_field(args[0])
                break

    @classmethod
    def wire(cls, backend: StorageBackend) -> None:
        """Bind *backend* to this storage class. Called by :class:`~skaal.runtime.local.LocalRuntime`."""
        cls._backend = backend

    @classmethod
    def _ensure_wired(cls) -> None:
        if cls._backend is None:
            raise NotImplementedError(
                f"{cls.__name__} storage not wired. Use LocalRuntime or deploy first."
            )

    @classmethod
    def _extract_key(cls, item: T) -> str:
        """Extract the primary key from *item* using ``cls.__skaal_key_field__``."""
        key_field = cls.__skaal_key_field__
        if hasattr(item, key_field):
            return str(getattr(item, key_field))
        if isinstance(item, dict) and key_field in item:
            return str(item[key_field])
        raise ValueError(
            f"Cannot extract key field {key_field!r} from {type(item).__name__}. "
            f"Set __skaal_key_field__ on the storage class to override."
        )

    # ── Async API (shared with Map) ───────────────────────────────────────

    @classmethod
    async def get(cls, key: str) -> T | None:
        """Return the item for *key*, or ``None``."""
        cls._ensure_wired()
        assert cls._backend is not None
        raw = await cls._backend.get(key)
        if cls.__skaal_value_type__ is not None:
            return _deserialize(raw, cls.__skaal_value_type__)
        return raw

    @classmethod
    async def set(cls, key: str, value: T) -> None:
        """Store *value* under *key*."""
        cls._ensure_wired()
        assert cls._backend is not None
        payload = (
            _serialize(value, cls.__skaal_value_type__)
            if cls.__skaal_value_type__ is not None
            else value
        )
        await cls._backend.set(key, payload)

    @classmethod
    async def delete(cls, key: str) -> None:
        """Remove *key*."""
        cls._ensure_wired()
        assert cls._backend is not None
        await cls._backend.delete(key)

    @classmethod
    async def list(cls) -> _List[tuple[str, T]]:
        """Return all ``(key, value)`` pairs."""
        cls._ensure_wired()
        assert cls._backend is not None
        entries = await cls._backend.list()
        if cls.__skaal_value_type__ is not None:
            return [(k, _deserialize(v, cls.__skaal_value_type__)) for k, v in entries]
        return entries

    @classmethod
    async def scan(cls, prefix: str = "") -> _List[tuple[str, T]]:
        """Return all ``(key, value)`` pairs matching *prefix*."""
        cls._ensure_wired()
        assert cls._backend is not None
        entries = await cls._backend.scan(prefix)
        if cls.__skaal_value_type__ is not None:
            return [(k, _deserialize(v, cls.__skaal_value_type__)) for k, v in entries]
        return entries

    @classmethod
    async def close(cls) -> None:
        """Release backend resources."""
        if cls._backend is not None:
            await cls._backend.close()

    # ── Collection-specific async API ─────────────────────────────────────

    @classmethod
    async def add(cls, item: T) -> None:
        """Store *item* using its auto-extracted primary key."""
        await cls.set(cls._extract_key(item), item)

    @classmethod
    async def remove(cls, key: str) -> None:
        """Remove the item with *key*. Alias for ``delete``."""
        await cls.delete(key)

    @classmethod
    async def update(cls, key: str, item: T) -> None:
        """Replace the value at *key* with *item*."""
        await cls.set(key, item)

    @classmethod
    async def all(cls) -> _List[T]:
        """Return all items as a flat list."""
        return [v for _, v in await cls.list()]

    @classmethod
    async def find(cls, prefix: str = "") -> _List[T]:
        """Return items whose key starts with *prefix*."""
        return [v for _, v in await cls.scan(prefix)]

    # ── Sync API ──────────────────────────────────────────────────────────

    @classmethod
    def sync_get(cls, key: str) -> T | None:
        """Synchronous wrapper for :meth:`get`."""
        from skaal.backends.local_backend import _sync_run

        return _sync_run(cls.get(key))

    @classmethod
    def sync_set(cls, key: str, value: T) -> None:
        """Synchronous wrapper for :meth:`set`."""
        from skaal.backends.local_backend import _sync_run

        _sync_run(cls.set(key, value))

    @classmethod
    def sync_delete(cls, key: str) -> None:
        """Synchronous wrapper for :meth:`delete`."""
        from skaal.backends.local_backend import _sync_run

        _sync_run(cls.delete(key))

    @classmethod
    def sync_list(cls) -> _List[tuple[str, T]]:
        """Synchronous wrapper for :meth:`list`."""
        from skaal.backends.local_backend import _sync_run

        return _sync_run(cls.list())

    @classmethod
    def sync_scan(cls, prefix: str = "") -> _List[tuple[str, T]]:
        """Synchronous wrapper for :meth:`scan`."""
        from skaal.backends.local_backend import _sync_run

        return _sync_run(cls.scan(prefix))

    @classmethod
    def sync_add(cls, item: T) -> None:
        """Synchronous wrapper for :meth:`add`."""
        from skaal.backends.local_backend import _sync_run

        _sync_run(cls.add(item))

    @classmethod
    def sync_all(cls) -> _List[T]:
        """Synchronous wrapper for :meth:`all`."""
        from skaal.backends.local_backend import _sync_run

        return _sync_run(cls.all())

    @classmethod
    def sync_find(cls, prefix: str = "") -> _List[T]:
        """Synchronous wrapper for :meth:`find`."""
        from skaal.backends.local_backend import _sync_run

        return _sync_run(cls.find(prefix))
