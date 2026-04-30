from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from skaal.blob import decode_blob_cursor, encode_blob_cursor, normalize_blob_limit
from skaal.types import BlobObject, Page


class FileBlobBackend:
    def __init__(self, root_path: str | Path, namespace: str | None = None) -> None:
        base = Path(root_path)
        self.root = base / namespace if namespace else base
        self._meta_root = self.root / ".skaal-meta"
        self.root.mkdir(parents=True, exist_ok=True)
        self._meta_root.mkdir(parents=True, exist_ok=True)

    def _normalize_key(self, key: str) -> str:
        normalized = key.replace("\\", "/")
        parts = [part for part in PurePosixPath(normalized).parts if part not in ("", ".")]
        if not parts or any(part == ".." for part in parts):
            raise ValueError(f"Invalid blob key: {key!r}")
        return "/".join(parts)

    def _data_path(self, key: str) -> Path:
        normalized = self._normalize_key(key)
        return self.root.joinpath(*normalized.split("/"))

    def _meta_path(self, key: str) -> Path:
        normalized = self._normalize_key(key)
        parts = normalized.split("/")
        return self._meta_root.joinpath(*parts[:-1], f"{parts[-1]}.json")

    def _build_object(self, key: str, data_path: Path, meta: dict[str, Any] | None) -> BlobObject:
        stat = data_path.stat()
        updated_raw = (meta or {}).get("updated_at")
        updated_at: datetime | None = None
        if isinstance(updated_raw, str):
            try:
                updated_at = datetime.fromisoformat(updated_raw)
            except ValueError:
                updated_at = None
        if updated_at is None:
            updated_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        return BlobObject(
            key=self._normalize_key(key),
            size=stat.st_size,
            content_type=(meta or {}).get("content_type"),
            etag=(meta or {}).get("etag"),
            updated_at=updated_at,
            metadata=dict((meta or {}).get("metadata") or {}),
        )

    def _read_meta(self, key: str) -> dict[str, Any] | None:
        meta_path = self._meta_path(key)
        if not meta_path.exists():
            return None
        return json.loads(meta_path.read_text(encoding="utf-8"))

    def _write_meta(
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
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
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
        data_path.parent.mkdir(parents=True, exist_ok=True)
        data_path.write_bytes(data)
        etag = hashlib.sha256(data).hexdigest()
        meta = self._write_meta(key, content_type=content_type, metadata=metadata, etag=etag)
        return self._build_object(key, data_path, meta)

    def _stat_sync(self, key: str) -> BlobObject | None:
        data_path = self._data_path(key)
        if not data_path.exists() or not data_path.is_file():
            return None
        return self._build_object(key, data_path, self._read_meta(key))

    def _delete_sync(self, key: str) -> None:
        data_path = self._data_path(key)
        meta_path = self._meta_path(key)
        if data_path.exists():
            data_path.unlink()
            parent = data_path.parent
            while parent != self.root and parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
        if meta_path.exists():
            meta_path.unlink()
            parent = meta_path.parent
            while parent != self._meta_root and parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent

    def _list_keys_sync(self, prefix: str) -> list[str]:
        keys: list[str] = []
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            if path.is_relative_to(self._meta_root):
                continue
            key = path.relative_to(self.root).as_posix()
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
        if not data_path.exists() or not data_path.is_file():
            raise FileNotFoundError(key)
        return await asyncio.to_thread(data_path.read_bytes)

    async def download_file(self, key: str, destination: str | Path) -> Path:
        data = await self.get_bytes(key)
        target = Path(destination)
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, data)
        return target

    async def stat(self, key: str) -> BlobObject | None:
        return await asyncio.to_thread(self._stat_sync, key)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(lambda: self._data_path(key).is_file())

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
        return None

    def __repr__(self) -> str:
        return f"FileBlobBackend(root={str(self.root)!r})"
