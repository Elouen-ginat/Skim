from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from skaal.blob import decode_blob_cursor, encode_blob_cursor, normalize_blob_limit
from skaal.types import BlobObject, Page


class GCSBlobBackend:
    def __init__(
        self, bucket: str, namespace: str | None = None, client: Any | None = None
    ) -> None:
        self.bucket_name = bucket
        self.namespace = namespace.strip("/") if namespace else ""
        if client is None:
            from google.cloud import storage

            client = storage.Client()
        self._client = client
        self._bucket = client.bucket(bucket)

    def _object_key(self, key: str) -> str:
        logical = key.strip("/")
        if not logical:
            raise ValueError("Blob key must not be empty")
        return f"{self.namespace}/{logical}" if self.namespace else logical

    def _logical_key(self, object_key: str) -> str:
        if self.namespace:
            prefix = f"{self.namespace}/"
            if object_key.startswith(prefix):
                return object_key[len(prefix) :]
        return object_key

    async def _call(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    def _build_object(self, blob: Any) -> BlobObject:
        updated_at = blob.updated if isinstance(getattr(blob, "updated", None), datetime) else None
        size = getattr(blob, "size", None)
        return BlobObject(
            key=self._logical_key(blob.name),
            size=int(size or 0),
            content_type=getattr(blob, "content_type", None),
            etag=getattr(blob, "etag", None),
            updated_at=updated_at,
            metadata=dict(getattr(blob, "metadata", None) or {}),
        )

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> BlobObject:
        blob = self._bucket.blob(self._object_key(key))
        blob.metadata = dict(metadata or {})
        blob.content_type = content_type
        await self._call(blob.upload_from_string, data, content_type=content_type)
        await self._call(blob.reload)
        return self._build_object(blob)

    async def put_file(
        self,
        key: str,
        source: str | Path,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> BlobObject:
        blob = self._bucket.blob(self._object_key(key))
        blob.metadata = dict(metadata or {})
        blob.content_type = content_type
        await self._call(blob.upload_from_filename, str(source), content_type=content_type)
        await self._call(blob.reload)
        return self._build_object(blob)

    async def get_bytes(self, key: str) -> bytes:
        blob = await self._call(self._bucket.get_blob, self._object_key(key))
        if blob is None:
            raise FileNotFoundError(key)
        return await self._call(blob.download_as_bytes)

    async def download_file(self, key: str, destination: str | Path) -> Path:
        blob = await self._call(self._bucket.get_blob, self._object_key(key))
        if blob is None:
            raise FileNotFoundError(key)
        target = Path(destination)
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await self._call(blob.download_to_filename, str(target))
        return target

    async def stat(self, key: str) -> BlobObject | None:
        blob = await self._call(self._bucket.get_blob, self._object_key(key))
        if blob is None:
            return None
        return self._build_object(blob)

    async def exists(self, key: str) -> bool:
        return await self.stat(key) is not None

    async def delete(self, key: str) -> None:
        blob = self._bucket.blob(self._object_key(key))
        try:
            await self._call(blob.delete)
        except Exception as exc:  # noqa: BLE001
            status_code = getattr(exc, "code", None)
            if status_code == 404:
                return
            raise

    async def list_page(
        self,
        prefix: str = "",
        *,
        limit: int,
        cursor: str | None,
    ) -> Page[BlobObject]:
        limit = normalize_blob_limit(limit)
        last_key = decode_blob_cursor(cursor, prefix=prefix)
        object_prefix = (
            self._object_key(prefix) if prefix else (f"{self.namespace}/" if self.namespace else "")
        )
        if hasattr(self._client, "list_blobs"):
            iterator = await self._call(
                self._client.list_blobs, self.bucket_name, prefix=object_prefix
            )
        else:
            iterator = await self._call(self._bucket.list_blobs, prefix=object_prefix)
        filtered: list[Any] = []
        last_object_key = self._object_key(last_key) if last_key is not None else None
        for blob in iterator:
            if last_object_key is not None and blob.name <= last_object_key:
                continue
            filtered.append(blob)
            if len(filtered) >= limit + 1:
                break
        has_more = len(filtered) > limit
        selected = filtered[:limit]
        items = [self._build_object(blob) for blob in selected]
        next_cursor = None
        if has_more and items:
            next_cursor = encode_blob_cursor(prefix=prefix, last_key=items[-1].key)
        return Page(items=items, next_cursor=next_cursor, has_more=has_more)

    async def close(self) -> None:
        return None

    def __repr__(self) -> str:
        return f"GCSBlobBackend(bucket={self.bucket_name!r}, namespace={self.namespace!r})"
