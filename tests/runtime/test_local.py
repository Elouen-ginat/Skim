"""Tests for the local in-process runtime."""

from __future__ import annotations

import json

import pytest

from skaal import App
from skaal.backends.file_blob_backend import FileBlobBackend
from skaal.backends.local_backend import LocalMap
from skaal.blob import BlobStore
from skaal.runtime.local import LocalRuntime
from skaal.storage import Store


def _invoke_path(app: App, function_name: str) -> str:
    return f"/_skaal/invoke/{app.name}.{function_name}"


# ── Storage tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_local_map_get_set_delete():
    m = LocalMap()
    assert await m.get("x") is None
    await m.set("x", 42)
    assert await m.get("x") == 42
    await m.delete("x")
    assert await m.get("x") is None


@pytest.mark.asyncio
async def test_local_map_list_scan():
    m = LocalMap()
    await m.set("a:1", "v1")
    await m.set("a:2", "v2")
    await m.set("b:1", "v3")
    all_items = await m.list()
    assert len(all_items) == 3
    scanned = await m.scan("a:")
    assert set(k for k, _ in scanned) == {"a:1", "a:2"}


# ── Runtime + dispatch tests ───────────────────────────────────────────────────


def _make_counter_app() -> App:
    """Build a fresh counter App instance for each test."""
    app = App("test-counter")

    @app.storage(read_latency="< 5ms", durability="ephemeral")
    class Counts(Store[int]):
        pass

    @app.function()
    async def increment(name: str, by: int = 1) -> dict:
        current = await Counts.get(name) or 0
        new_value = current + by
        await Counts.set(name, new_value)
        return {"name": name, "value": new_value}

    @app.function()
    async def get_count(name: str) -> dict:
        value = await Counts.get(name) or 0
        return {"name": name, "value": value}

    @app.function()
    async def list_counts() -> dict:
        entries = await Counts.list()
        return {"counts": dict(entries)}

    return app


@pytest.mark.asyncio
async def test_runtime_index():
    runtime = LocalRuntime(_make_counter_app())
    data, status = await runtime._dispatch("GET", "/", b"")
    assert status == 200
    assert "endpoints" in data
    names = [e["function"] for e in data["endpoints"]]
    assert "test-counter.increment" in names
    assert "test-counter.get_count" in names


@pytest.mark.asyncio
async def test_runtime_health():
    runtime = LocalRuntime(_make_counter_app())
    data, status = await runtime._dispatch("GET", "/health", b"")
    assert status == 200
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_runtime_function_call():
    runtime = LocalRuntime(_make_counter_app())
    body = json.dumps({"name": "hits"}).encode()
    data, status = await runtime._dispatch("POST", _invoke_path(runtime.app, "increment"), body)
    assert status == 200
    assert data["name"] == "hits"
    assert data["value"] == 1

    # Increment again by 5
    body2 = json.dumps({"name": "hits", "by": 5}).encode()
    data2, status2 = await runtime._dispatch("POST", _invoke_path(runtime.app, "increment"), body2)
    assert status2 == 200
    assert data2["value"] == 6


@pytest.mark.asyncio
async def test_runtime_storage_persists_across_calls():
    runtime = LocalRuntime(_make_counter_app())

    await runtime._dispatch(
        "POST", _invoke_path(runtime.app, "increment"), json.dumps({"name": "a"}).encode()
    )
    await runtime._dispatch(
        "POST", _invoke_path(runtime.app, "increment"), json.dumps({"name": "a"}).encode()
    )
    await runtime._dispatch(
        "POST", _invoke_path(runtime.app, "increment"), json.dumps({"name": "b"}).encode()
    )

    data, status = await runtime._dispatch("POST", _invoke_path(runtime.app, "list_counts"), b"")
    assert status == 200
    assert data["counts"]["a"] == 2
    assert data["counts"]["b"] == 1


@pytest.mark.asyncio
async def test_local_runtime_wires_blob_store(tmp_path):
    app = App("blob-runtime")

    @app.blob(read_latency="< 50ms", durability="durable")
    class Uploads(BlobStore):
        pass

    runtime = LocalRuntime(
        app,
        backend_overrides={"Uploads": FileBlobBackend(tmp_path / "runtime-blobs")},
    )

    assert isinstance(runtime._backends["blob-runtime.Uploads"], FileBlobBackend)

    await Uploads.put_bytes("notes/one.txt", b"hello")
    assert await Uploads.get_bytes("notes/one.txt") == b"hello"


@pytest.mark.asyncio
async def test_runtime_unknown_function():
    runtime = LocalRuntime(_make_counter_app())
    data, status = await runtime._dispatch("POST", "/_skaal/invoke/test-counter.no_such_fn", b"{}")
    assert status == 404
    assert "error" in data


@pytest.mark.asyncio
async def test_runtime_invalid_json():
    runtime = LocalRuntime(_make_counter_app())
    data, status = await runtime._dispatch(
        "POST", _invoke_path(runtime.app, "increment"), b"not json"
    )
    assert status == 400
    assert "error" in data


@pytest.mark.asyncio
async def test_runtime_bad_args():
    runtime = LocalRuntime(_make_counter_app())
    # increment() requires 'name', send nothing
    data, status = await runtime._dispatch("POST", _invoke_path(runtime.app, "increment"), b"{}")
    assert status == 422
    assert "error" in data


@pytest.mark.asyncio
async def test_runtime_method_not_allowed():
    runtime = LocalRuntime(_make_counter_app())
    data, status = await runtime._dispatch("DELETE", _invoke_path(runtime.app, "increment"), b"")
    assert status == 405


@pytest.mark.asyncio
async def test_runtime_legacy_public_function_route_is_gone():
    runtime = LocalRuntime(_make_counter_app())
    data, status = await runtime._dispatch(
        "POST", "/increment", json.dumps({"name": "hits"}).encode()
    )
    assert status == 404
    assert "error" in data


# ── End-to-end: actual TCP server ─────────────────────────────────────────────

# Note: these tests are a bit more fragile since they depend on the full server stack. don't work in CI
# @pytest.mark.asyncio
# async def test_end_to_end_tcp():
#     """Spin up a real server on a random port, hit it over TCP."""
#     import socket

#     # Find a free port
#     with socket.socket() as s:
#         s.bind(("127.0.0.1", 0))
#         port = s.getsockname()[1]

#     runtime = LocalRuntime(_make_counter_app(), port=port)
#     server_task = asyncio.create_task(runtime.serve())

#     # Give the server a moment to start
#     await asyncio.sleep(0.05)

#     try:
#         reader, writer = await asyncio.open_connection("127.0.0.1", port)

#         body = json.dumps({"name": "tcp_test"}).encode()
#         request = (
#             f"POST /increment HTTP/1.1\r\n"
#             f"Host: localhost\r\n"
#             f"Content-Type: application/json\r\n"
#             f"Content-Length: {len(body)}\r\n"
#             f"Connection: close\r\n"
#             f"\r\n"
#         ).encode() + body

#         writer.write(request)
#         await writer.drain()

#         response_raw = b""
#         while True:
#             chunk = await asyncio.wait_for(reader.read(4096), timeout=5.0)
#             if not chunk:
#                 break
#             response_raw += chunk

#         writer.close()
#         await writer.wait_closed()

#         # Parse response
#         header_end = response_raw.find(b"\r\n\r\n")
#         assert header_end != -1
#         response_body = response_raw[header_end + 4 :]
#         response_data = json.loads(response_body)

#         assert response_data["name"] == "tcp_test"
#         assert response_data["value"] == 1
#     finally:
#         server_task.cancel()
#         try:
#             await server_task
#         except asyncio.CancelledError:
#             pass
