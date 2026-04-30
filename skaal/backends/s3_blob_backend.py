from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from skaal.blob import decode_blob_cursor, encode_blob_cursor, normalize_blob_limit
from skaal.types import BlobObject, Page


class S3BlobBackend:
    def __init__(
        self, bucket: str, namespace: str | None = None, client: Any | None = None
    ) -> None:
        self.bucket = bucket
        self.namespace = namespace.strip("/") if namespace else ""
        if client is None:
            import boto3

            client = boto3.client("s3")
        self._client = client

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

    def _coerce_updated(self, value: Any) -> datetime | None:
        return value if isinstance(value, datetime) else None

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> BlobObject:
        kwargs: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": self._object_key(key),
            "Body": data,
            "Metadata": dict(metadata or {}),
        }
        if content_type is not None:
            kwargs["ContentType"] = content_type
        await self._call(self._client.put_object, **kwargs)
        obj = await self.stat(key)
        if obj is None:
            raise FileNotFoundError(key)
        return obj

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
        response = await self._call(
            self._client.get_object,
            Bucket=self.bucket,
            Key=self._object_key(key),
        )
        body = response["Body"]
        return await asyncio.to_thread(body.read)

    async def download_file(self, key: str, destination: str | Path) -> Path:
        data = await self.get_bytes(key)
        target = Path(destination)
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, data)
        return target

    async def stat(self, key: str) -> BlobObject | None:
        try:
            head = await self._call(
                self._client.head_object,
                Bucket=self.bucket,
                Key=self._object_key(key),
            )
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        etag = head.get("ETag")
        if isinstance(etag, str):
            etag = etag.strip('"')
        return BlobObject(
            key=key.strip("/"),
            size=int(head.get("ContentLength", 0)),
            content_type=head.get("ContentType"),
            etag=etag,
            updated_at=self._coerce_updated(head.get("LastModified")),
            metadata=dict(head.get("Metadata") or {}),
        )

    async def exists(self, key: str) -> bool:
        return await self.stat(key) is not None

    async def delete(self, key: str) -> None:
        await self._call(self._client.delete_object, Bucket=self.bucket, Key=self._object_key(key))

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
        kwargs: dict[str, Any] = {
            "Bucket": self.bucket,
            "Prefix": object_prefix,
            "MaxKeys": limit + 1,
        }
        if last_key is not None:
            kwargs["StartAfter"] = self._object_key(last_key)
        response = await self._call(self._client.list_objects_v2, **kwargs)
        contents = list(response.get("Contents") or [])
        has_more = len(contents) > limit
        selected = contents[:limit]
        items = [
            BlobObject(
                key=self._logical_key(item["Key"]),
                size=int(item.get("Size", 0)),
                content_type=None,
                etag=str(item.get("ETag", "")).strip('"') or None,
                updated_at=self._coerce_updated(item.get("LastModified")),
                metadata={},
            )
            for item in selected
        ]
        next_cursor = None
        if has_more and items:
            next_cursor = encode_blob_cursor(prefix=prefix, last_key=items[-1].key)
        return Page(items=items, next_cursor=next_cursor, has_more=has_more)

    async def close(self) -> None:
        return None

    def __repr__(self) -> str:
        return f"S3BlobBackend(bucket={self.bucket!r}, namespace={self.namespace!r})"
