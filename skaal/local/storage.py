"""In-memory storage backends for local development."""

from __future__ import annotations

from typing import Any


class LocalMap:
    """
    In-memory key-value store.

    Patched onto storage classes during ``LocalRuntime`` startup so that
    user code can call ``await MyStorage.get(key)`` without any real backend.

    All methods are async to match the production backend interface.
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

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"LocalMap({len(self._data)} keys)"


def patch_storage_class(cls: type, backend: LocalMap) -> None:
    """
    Inject ``backend`` as class-level async methods on ``cls``.

    After patching, user code can call:

        value = await MyStorage.get("key")
        await MyStorage.set("key", value)
        await MyStorage.delete("key")
        entries = await MyStorage.list()
        results = await MyStorage.scan("prefix:")

    The backend is also available as ``cls._local`` for direct access.
    """
    cls._local = backend  # type: ignore[attr-defined]

    # Each method closes over its own backend instance to avoid loop-variable bugs.
    async def _get(key: str) -> Any | None:
        return await backend.get(key)

    async def _set(key: str, value: Any) -> None:
        await backend.set(key, value)

    async def _delete(key: str) -> None:
        await backend.delete(key)

    async def _list() -> list[tuple[str, Any]]:
        return await backend.list()

    async def _scan(prefix: str = "") -> list[tuple[str, Any]]:
        return await backend.scan(prefix)

    cls.get = staticmethod(_get)      # type: ignore[attr-defined]
    cls.set = staticmethod(_set)      # type: ignore[attr-defined]
    cls.delete = staticmethod(_delete)  # type: ignore[attr-defined]
    cls.list = staticmethod(_list)    # type: ignore[attr-defined]
    cls.scan = staticmethod(_scan)    # type: ignore[attr-defined]
