from __future__ import annotations

from pathlib import Path

import pytest
from fsspec.implementations.memory import MemoryFileSystem

from skaal.backends.base import BlobBackend
from skaal.backends.file_blob_backend import FileBlobBackend
from skaal.backends.gcs_blob_backend import GCSBlobBackend
from skaal.backends.s3_blob_backend import S3BlobBackend


def _local_backend(tmp_path: Path) -> FileBlobBackend:
    return FileBlobBackend(tmp_path / "blob-contract")


def _s3_backend(tmp_path: Path) -> S3BlobBackend:
    del tmp_path
    return S3BlobBackend("test-bucket", namespace="contract", filesystem=MemoryFileSystem())


def _gcs_backend(tmp_path: Path) -> GCSBlobBackend:
    del tmp_path
    return GCSBlobBackend("test-bucket", namespace="contract", filesystem=MemoryFileSystem())


@pytest.fixture(params=[_local_backend, _s3_backend, _gcs_backend])
def blob_backend(request: pytest.FixtureRequest, tmp_path: Path) -> BlobBackend:
    factory = request.param
    return factory(tmp_path)


def test_blob_backends_implement_protocol(tmp_path: Path) -> None:
    assert isinstance(FileBlobBackend(tmp_path / "blob"), BlobBackend)
    assert isinstance(S3BlobBackend("bucket", filesystem=MemoryFileSystem()), BlobBackend)
    assert isinstance(GCSBlobBackend("bucket", filesystem=MemoryFileSystem()), BlobBackend)


def test_file_blob_backend_preserves_posix_absolute_root() -> None:
    backend = FileBlobBackend("/app/data/blobs", namespace="Uploads")
    root_path = backend._root_path.replace("\\", "/")
    data_path = backend._data_path("uploads/readme.txt").replace("\\", "/")
    meta_path = backend._meta_path("uploads/readme.txt").replace("\\", "/")

    assert root_path.endswith("/app/data/blobs/Uploads")
    assert data_path.endswith("/app/data/blobs/Uploads/uploads/readme.txt")
    assert meta_path.endswith("/app/data/blobs/Uploads/.skaal-meta/uploads/readme.txt.json")
    assert "/app/app/data/" not in data_path


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
