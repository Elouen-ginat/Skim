from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Any

import pytest

from skaal.backends.base import BlobBackend
from skaal.backends.file_blob_backend import FileBlobBackend
from skaal.backends.gcs_blob_backend import GCSBlobBackend
from skaal.backends.s3_blob_backend import S3BlobBackend


class FakeS3Error(Exception):
    def __init__(self, code: str) -> None:
        self.response = {"Error": {"Code": code}}
        super().__init__(code)


class FakeS3Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        Metadata: dict[str, str] | None = None,
        ContentType: str | None = None,
    ) -> None:
        self.objects[(Bucket, Key)] = {
            "Body": Body,
            "Metadata": dict(Metadata or {}),
            "ContentType": ContentType,
            "ETag": sha1(Body).hexdigest(),
            "LastModified": datetime.now(timezone.utc),
            "ContentLength": len(Body),
        }

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        item = self.objects[(Bucket, Key)]
        return {"Body": FakeS3Body(item["Body"])}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        item = self.objects.get((Bucket, Key))
        if item is None:
            raise FakeS3Error("404")
        return {
            "Metadata": item["Metadata"],
            "ContentType": item["ContentType"],
            "ETag": f'"{item["ETag"]}"',
            "LastModified": item["LastModified"],
            "ContentLength": item["ContentLength"],
        }

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.objects.pop((Bucket, Key), None)

    def list_objects_v2(
        self,
        *,
        Bucket: str,
        Prefix: str,
        MaxKeys: int,
        StartAfter: str | None = None,
    ) -> dict[str, Any]:
        keys = [
            key
            for (bucket, key), value in sorted(self.objects.items())
            if bucket == Bucket
            and key.startswith(Prefix)
            and (StartAfter is None or key > StartAfter)
        ]
        contents = []
        for key in keys[:MaxKeys]:
            item = self.objects[(Bucket, key)]
            contents.append(
                {
                    "Key": key,
                    "Size": item["ContentLength"],
                    "ETag": f'"{item["ETag"]}"',
                    "LastModified": item["LastModified"],
                }
            )
        return {"Contents": contents}


@dataclass
class _FakeGCSObject:
    data: bytes
    metadata: dict[str, str]
    content_type: str | None
    updated: datetime
    etag: str


class FakeGCSBlob:
    def __init__(self, bucket: "FakeGCSBucket", name: str) -> None:
        self._bucket = bucket
        self.name = name
        self.metadata: dict[str, str] | None = None
        self.content_type: str | None = None

    @property
    def _entry(self) -> _FakeGCSObject:
        return self._bucket.objects[self.name]

    @property
    def metadata(self) -> dict[str, str] | None:
        if self.name in self._bucket.objects:
            return dict(self._entry.metadata)
        return self.__dict__.get("_metadata")

    @metadata.setter
    def metadata(self, value: dict[str, str] | None) -> None:
        self.__dict__["_metadata"] = dict(value or {})

    @property
    def content_type(self) -> str | None:
        if self.name in self._bucket.objects:
            return self._entry.content_type
        return self.__dict__.get("_content_type")

    @content_type.setter
    def content_type(self, value: str | None) -> None:
        self.__dict__["_content_type"] = value

    @property
    def updated(self) -> datetime | None:
        return (
            self._bucket.objects.get(self.name).updated
            if self.name in self._bucket.objects
            else None
        )

    @property
    def etag(self) -> str | None:
        return (
            self._bucket.objects.get(self.name).etag if self.name in self._bucket.objects else None
        )

    @property
    def size(self) -> int | None:
        return len(self._entry.data) if self.name in self._bucket.objects else None

    def upload_from_string(self, data: bytes, content_type: str | None = None) -> None:
        self._bucket.objects[self.name] = _FakeGCSObject(
            data=data,
            metadata=dict(self.metadata or {}),
            content_type=content_type,
            updated=datetime.now(timezone.utc),
            etag=sha1(data).hexdigest(),
        )

    def upload_from_filename(self, filename: str, content_type: str | None = None) -> None:
        self.upload_from_string(Path(filename).read_bytes(), content_type=content_type)

    def reload(self) -> None:
        return None

    def download_as_bytes(self) -> bytes:
        return self._entry.data

    def download_to_filename(self, filename: str) -> None:
        Path(filename).write_bytes(self._entry.data)

    def delete(self) -> None:
        self._bucket.objects.pop(self.name, None)


class FakeGCSBucket:
    def __init__(self) -> None:
        self.objects: dict[str, _FakeGCSObject] = {}

    def blob(self, name: str) -> FakeGCSBlob:
        return FakeGCSBlob(self, name)

    def get_blob(self, name: str) -> FakeGCSBlob | None:
        if name not in self.objects:
            return None
        return FakeGCSBlob(self, name)

    def list_blobs(self, prefix: str = "") -> list[FakeGCSBlob]:
        return [FakeGCSBlob(self, name) for name in sorted(self.objects) if name.startswith(prefix)]


class FakeGCSClient:
    def __init__(self) -> None:
        self.buckets: dict[str, FakeGCSBucket] = {}

    def bucket(self, name: str) -> FakeGCSBucket:
        return self.buckets.setdefault(name, FakeGCSBucket())

    def list_blobs(self, bucket_name: str, prefix: str = "") -> list[FakeGCSBlob]:
        return self.bucket(bucket_name).list_blobs(prefix=prefix)


def _local_backend(tmp_path: Path) -> FileBlobBackend:
    return FileBlobBackend(tmp_path / "blob-contract")


def _s3_backend(tmp_path: Path) -> S3BlobBackend:
    del tmp_path
    return S3BlobBackend("test-bucket", namespace="contract", client=FakeS3Client())


def _gcs_backend(tmp_path: Path) -> GCSBlobBackend:
    del tmp_path
    return GCSBlobBackend("test-bucket", namespace="contract", client=FakeGCSClient())


@pytest.fixture(params=[_local_backend, _s3_backend, _gcs_backend])
def blob_backend(request: pytest.FixtureRequest, tmp_path: Path) -> BlobBackend:
    factory = request.param
    return factory(tmp_path)


def test_blob_backends_implement_protocol(tmp_path: Path) -> None:
    assert isinstance(FileBlobBackend(tmp_path / "blob"), BlobBackend)
    assert isinstance(S3BlobBackend("bucket", client=FakeS3Client()), BlobBackend)
    assert isinstance(GCSBlobBackend("bucket", client=FakeGCSClient()), BlobBackend)


@pytest.mark.asyncio
async def test_put_get_stat_list_delete(blob_backend: BlobBackend, tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_bytes(b"hello world")

    created = await blob_backend.put_file(
        "docs/source.txt",
        source,
        content_type="text/plain",
        metadata={"owner": "blob-test"},
    )
    assert created.key == "docs/source.txt"

    assert await blob_backend.get_bytes("docs/source.txt") == b"hello world"

    stat = await blob_backend.stat("docs/source.txt")
    assert stat is not None
    assert stat.content_type == "text/plain"
    assert stat.metadata == {"owner": "blob-test"}

    await blob_backend.put_bytes("docs/second.txt", b"second")
    page = await blob_backend.list_page(prefix="docs/", limit=1, cursor=None)
    assert [item.key for item in page.items] == ["docs/second.txt"] or [
        item.key for item in page.items
    ] == ["docs/source.txt"]
    assert page.has_more is True
    assert page.next_cursor is not None

    next_page = await blob_backend.list_page(prefix="docs/", limit=10, cursor=page.next_cursor)
    assert len(next_page.items) == 1

    destination = tmp_path / "download" / "out.txt"
    await blob_backend.download_file("docs/source.txt", destination)
    assert destination.read_bytes() == b"hello world"

    assert await blob_backend.exists("docs/source.txt") is True
    await blob_backend.delete("docs/source.txt")
    assert await blob_backend.exists("docs/source.txt") is False
