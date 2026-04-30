from __future__ import annotations

from pathlib import Path

import pytest

from skaal import App, BlobStore
from skaal.backends.file_blob_backend import FileBlobBackend
from skaal.runtime.local import LocalRuntime


def _make_blob_app(tmp_path: Path) -> tuple[App, LocalRuntime, type[BlobStore]]:
    app = App("blob-demo")

    @app.blob(read_latency="< 50ms", durability="durable")
    class Uploads(BlobStore):
        pass

    runtime = LocalRuntime(
        app,
        backend_overrides={"Uploads": FileBlobBackend(tmp_path / "blob-store")},
    )
    return app, runtime, Uploads


@pytest.mark.asyncio
async def test_blob_store_put_get_list_stat_delete(tmp_path: Path) -> None:
    _app, _runtime, Uploads = _make_blob_app(tmp_path)

    first = await Uploads.put_bytes(
        "avatars/alice.txt",
        b"alice",
        content_type="text/plain",
        metadata={"owner": "alice"},
    )
    await Uploads.put_bytes("avatars/bob.txt", b"bob", content_type="text/plain")

    assert first.key == "avatars/alice.txt"
    assert await Uploads.get_bytes("avatars/alice.txt") == b"alice"

    stat = await Uploads.stat("avatars/alice.txt")
    assert stat is not None
    assert stat.content_type == "text/plain"
    assert stat.metadata == {"owner": "alice"}

    page1 = await Uploads.list_page(prefix="avatars/", limit=1)
    assert [item.key for item in page1.items] == ["avatars/alice.txt"]
    assert page1.has_more is True

    page2 = await Uploads.list_page(prefix="avatars/", limit=1, cursor=page1.next_cursor)
    assert [item.key for item in page2.items] == ["avatars/bob.txt"]
    assert page2.has_more is False

    assert await Uploads.exists("avatars/alice.txt") is True
    await Uploads.delete("avatars/alice.txt")
    assert await Uploads.exists("avatars/alice.txt") is False
    assert await Uploads.stat("avatars/alice.txt") is None


def test_blob_store_sync_helpers(tmp_path: Path) -> None:
    _app, _runtime, Uploads = _make_blob_app(tmp_path)

    created = Uploads.sync_put_bytes("reports/q1.txt", b"quarterly report")
    assert created.key == "reports/q1.txt"
    assert Uploads.sync_get_bytes("reports/q1.txt") == b"quarterly report"

    destination = tmp_path / "downloads" / "q1.txt"
    downloaded = Uploads.sync_download_file("reports/q1.txt", destination)
    assert downloaded == destination
    assert destination.read_bytes() == b"quarterly report"

    listed = Uploads.sync_list(prefix="reports/")
    assert [item.key for item in listed] == ["reports/q1.txt"]
