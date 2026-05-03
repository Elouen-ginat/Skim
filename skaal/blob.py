"""Typed blob/object storage surface."""

from __future__ import annotations

import builtins
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from skaal.storage import _decode_cursor, _encode_cursor, _normalize_limit
from skaal.sync import run as _sync_run
from skaal.types import BlobObject, Page

if TYPE_CHECKING:
    from skaal.backends.base import BlobBackend


def is_blob_model(obj: Any) -> bool:
    return (
        isinstance(obj, type)
        and hasattr(obj, "__skaal_storage__")
        and getattr(obj, "__skaal_storage__", {}).get("kind") == "blob"
    )


def validate_blob_model(store_cls: type) -> None:
    if not isinstance(store_cls, type) or not issubclass(store_cls, BlobStore):
        raise TypeError('@app.storage(kind="blob") requires a skaal.BlobStore subclass.')


class BlobStore:
    _backend: ClassVar[BlobBackend | None] = None

    @classmethod
    def wire(cls, backend: BlobBackend) -> None:
        cls._backend = backend

    @classmethod
    def _ensure_wired(cls) -> None:
        if cls._backend is None:
            raise NotImplementedError(
                f"{cls.__name__} blob store not wired. Use LocalRuntime or deploy first."
            )

    @classmethod
    async def put_bytes(
        cls,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> BlobObject:
        cls._ensure_wired()
        assert cls._backend is not None
        return await cls._backend.put_bytes(
            key,
            data,
            content_type=content_type,
            metadata=metadata,
        )

    @classmethod
    async def put_file(
        cls,
        key: str,
        source: str | Path,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> BlobObject:
        cls._ensure_wired()
        assert cls._backend is not None
        return await cls._backend.put_file(
            key,
            source,
            content_type=content_type,
            metadata=metadata,
        )

    @classmethod
    async def get_bytes(cls, key: str) -> bytes:
        cls._ensure_wired()
        assert cls._backend is not None
        return await cls._backend.get_bytes(key)

    @classmethod
    async def download_file(cls, key: str, destination: str | Path) -> Path:
        cls._ensure_wired()
        assert cls._backend is not None
        return await cls._backend.download_file(key, destination)

    @classmethod
    async def stat(cls, key: str) -> BlobObject | None:
        cls._ensure_wired()
        assert cls._backend is not None
        return await cls._backend.stat(key)

    @classmethod
    async def exists(cls, key: str) -> bool:
        cls._ensure_wired()
        assert cls._backend is not None
        return await cls._backend.exists(key)

    @classmethod
    async def delete(cls, key: str) -> None:
        cls._ensure_wired()
        assert cls._backend is not None
        await cls._backend.delete(key)

    @classmethod
    async def list_page(
        cls,
        prefix: str = "",
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> Page[BlobObject]:
        cls._ensure_wired()
        assert cls._backend is not None
        return await cls._backend.list_page(prefix=prefix, limit=limit, cursor=cursor)

    @classmethod
    async def list(cls, prefix: str = "") -> builtins.list[BlobObject]:
        items: builtins.list[BlobObject] = []
        cursor: str | None = None
        while True:
            page = await cls.list_page(prefix=prefix, limit=1000, cursor=cursor)
            items.extend(page.items)
            if not page.has_more:
                return items
            cursor = page.next_cursor

    @classmethod
    def sync_put_bytes(
        cls,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> BlobObject:
        return _sync_run(cls.put_bytes(key, data, content_type=content_type, metadata=metadata))

    @classmethod
    def sync_put_file(
        cls,
        key: str,
        source: str | Path,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> BlobObject:
        return _sync_run(cls.put_file(key, source, content_type=content_type, metadata=metadata))

    @classmethod
    def sync_get_bytes(cls, key: str) -> bytes:
        return _sync_run(cls.get_bytes(key))

    @classmethod
    def sync_download_file(cls, key: str, destination: str | Path) -> Path:
        return _sync_run(cls.download_file(key, destination))

    @classmethod
    def sync_stat(cls, key: str) -> BlobObject | None:
        return _sync_run(cls.stat(key))

    @classmethod
    def sync_exists(cls, key: str) -> bool:
        return _sync_run(cls.exists(key))

    @classmethod
    def sync_delete(cls, key: str) -> None:
        _sync_run(cls.delete(key))

    @classmethod
    def sync_list_page(
        cls,
        prefix: str = "",
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> Page[BlobObject]:
        return _sync_run(cls.list_page(prefix=prefix, limit=limit, cursor=cursor))

    @classmethod
    def sync_list(cls, prefix: str = "") -> builtins.list[BlobObject]:
        return _sync_run(cls.list(prefix=prefix))


def encode_blob_cursor(*, prefix: str, last_key: str) -> str:
    return _encode_cursor({"mode": "blob", "prefix": prefix, "last_key": last_key})


def decode_blob_cursor(cursor: str | None, *, prefix: str) -> str | None:
    if cursor is None:
        return None
    decoded = _decode_cursor(cursor)
    if decoded.get("mode") != "blob" or decoded.get("prefix") != prefix:
        raise ValueError("Cursor does not match this blob listing")
    last_key = decoded.get("last_key")
    if last_key is None:
        return None
    if not isinstance(last_key, str):
        raise ValueError("Invalid blob cursor")
    return last_key


def normalize_blob_limit(limit: int) -> int:
    return _normalize_limit(limit)
