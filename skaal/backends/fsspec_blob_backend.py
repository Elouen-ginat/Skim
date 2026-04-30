from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from fsspec.spec import AbstractFileSystem

from skaal.blob import decode_blob_cursor, encode_blob_cursor, normalize_blob_limit
from skaal.types import BlobObject, Page


class FsspecBlobBackend:
    def __init__(
        self,
        filesystem: AbstractFileSystem,
        root_path: str,
        namespace: str | None = None,
    ) -> None:
        self._filesystem = filesystem
        self.namespace = namespace.strip("/") if namespace else ""
        self._root_path = self._join_path(root_path, self.namespace)
        self._meta_root = self._join_path(self._root_path, ".skaal-meta")
        self._ensure_directory(self._root_path)
        self._ensure_directory(self._meta_root)

    def _normalize_key(self, key: str) -> str:
        normalized = key.replace("\\", "/")
        parts = [part for part in PurePosixPath(normalized).parts if part not in ("", ".")]
        if not parts or any(part == ".." for part in parts):
            raise ValueError(f"Invalid blob key: {key!r}")
        return "/".join(parts)

    def _normalize_path(self, path: str) -> str:
        normalized = path.replace("\\", "/")
        pure_path = PurePosixPath(normalized)
        parts = [part for part in pure_path.parts if part not in ("", ".")]
        if normalized.startswith("/"):
            cleaned = [part for part in parts if part != "/"]
            return PurePosixPath("/", *cleaned).as_posix()
        return PurePosixPath(*parts).as_posix()

    def _join_path(self, base: str, *parts: str) -> str:
        path = PurePosixPath(self._normalize_path(base))
        for part in parts:
            if part:
                path = path.joinpath(*part.split("/"))
        return path.as_posix()

    def _data_path(self, key: str) -> str:
        return self._join_path(self._root_path, self._normalize_key(key))

    def _meta_path(self, key: str) -> str:
        normalized = self._normalize_key(key)
        parts = normalized.split("/")
        return self._join_path(self._meta_root, *parts[:-1], f"{parts[-1]}.json")

    def _ensure_directory(self, path: str) -> None:
        try:
            self._filesystem.makedirs(path, exist_ok=True)
        except Exception:
            pass

    def _ensure_parent(self, path: str) -> None:
        parent = str(PurePosixPath(path).parent)
        if parent not in {"", "."}:
            self._ensure_directory(parent)

    def _coerce_updated(self, value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return None
        return None

    def _build_object(
        self,
        key: str,
        info: dict[str, Any],
        meta: dict[str, Any] | None,
    ) -> BlobObject:
        size = info.get("size", 0)
        updated_at = self._coerce_updated((meta or {}).get("updated_at"))
        if updated_at is None:
            updated_at = self._coerce_updated(
                info.get("created") or info.get("mtime") or info.get("updated")
            )
        return BlobObject(
            key=self._normalize_key(key),
            size=int(size if isinstance(size, int | float) else 0),
            content_type=(meta or {}).get("content_type"),
            etag=(meta or {}).get("etag"),
            updated_at=updated_at,
            metadata=dict((meta or {}).get("metadata") or {}),
        )

    def _read_meta_sync(self, key: str) -> dict[str, Any] | None:
        meta_path = self._meta_path(key)
        if not self._filesystem.exists(meta_path):
            return None
        with self._filesystem.open(meta_path, "rb") as handle:
            return json.loads(handle.read().decode("utf-8"))

    def _write_meta_sync(
        self,
        key: str,
        *,
        content_type: str | None,
        metadata: dict[str, str] | None,
        etag: str,
    ) -> dict[str, Any]:
        payload = {
            "content_type": content_type,
            "etag": etag,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "metadata": dict(metadata or {}),
        }
        meta_path = self._meta_path(key)
        self._ensure_parent(meta_path)
        with self._filesystem.open(meta_path, "wb") as handle:
            handle.write(json.dumps(payload, sort_keys=True).encode("utf-8"))
        return payload

    def _put_bytes_sync(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None,
        metadata: dict[str, str] | None,
    ) -> BlobObject:
        data_path = self._data_path(key)
        self._ensure_parent(data_path)
        with self._filesystem.open(data_path, "wb") as handle:
            handle.write(data)
        etag = hashlib.sha256(data).hexdigest()
        meta = self._write_meta_sync(key, content_type=content_type, metadata=metadata, etag=etag)
        return self._build_object(key, self._filesystem.info(data_path), meta)

    def _stat_sync(self, key: str) -> BlobObject | None:
        data_path = self._data_path(key)
        if not self._filesystem.exists(data_path):
            return None
        return self._build_object(key, self._filesystem.info(data_path), self._read_meta_sync(key))

    def _delete_sync(self, key: str) -> None:
        for path in (self._data_path(key), self._meta_path(key)):
            if self._filesystem.exists(path):
                self._filesystem.rm(path)

    def _list_keys_sync(self, prefix: str) -> list[str]:
        if not self._filesystem.exists(self._root_path):
            return []
        try:
            paths = list(self._filesystem.find(self._root_path, withdirs=False))
        except FileNotFoundError:
            return []
        root_prefix = f"{self._root_path}/"
        meta_prefix = f"{self._meta_root}/"
        keys: list[str] = []
        for raw_path in paths:
            path = self._normalize_path(str(raw_path))
            if path.startswith(meta_prefix):
                continue
            if path == self._root_path:
                continue
            if not path.startswith(root_prefix):
                continue
            key = path[len(root_prefix) :]
            if key.startswith(prefix):
                keys.append(key)
        keys.sort()
        return keys

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> BlobObject:
        return await asyncio.to_thread(
            self._put_bytes_sync,
            key,
            data,
            content_type=content_type,
            metadata=metadata,
        )

    async def put_file(
        self,
        key: str,
        source: str | Path,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> BlobObject:
        data = await asyncio.to_thread(Path(source).read_bytes)
        return await self.put_bytes(key, data, content_type=content_type, metadata=metadata)

    async def get_bytes(self, key: str) -> bytes:
        data_path = self._data_path(key)
        if not await asyncio.to_thread(self._filesystem.exists, data_path):
            raise FileNotFoundError(key)
        return await asyncio.to_thread(self._filesystem.cat_file, data_path)

    async def download_file(self, key: str, destination: str | Path) -> Path:
        data = await self.get_bytes(key)
        target = Path(destination)
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, data)
        return target

    async def stat(self, key: str) -> BlobObject | None:
        return await asyncio.to_thread(self._stat_sync, key)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._filesystem.exists, self._data_path(key))

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._delete_sync, key)

    async def list_page(
        self,
        prefix: str = "",
        *,
        limit: int,
        cursor: str | None,
    ) -> Page[BlobObject]:
        limit = normalize_blob_limit(limit)
        last_key = decode_blob_cursor(cursor, prefix=prefix)
        keys = await asyncio.to_thread(self._list_keys_sync, prefix)
        filtered = [key for key in keys if last_key is None or key > last_key]
        page_keys = filtered[: limit + 1]
        has_more = len(page_keys) > limit
        selected = page_keys[:limit]
        items = [
            item for item in await asyncio.gather(*(self.stat(key) for key in selected)) if item
        ]
        next_cursor = None
        if has_more and selected:
            next_cursor = encode_blob_cursor(prefix=prefix, last_key=selected[-1])
        return Page(items=items, next_cursor=next_cursor, has_more=has_more)

    async def close(self) -> None:
        close = getattr(self._filesystem, "close", None)
        if callable(close):
            await asyncio.to_thread(close)
