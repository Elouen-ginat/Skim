"""Tests for the local in-process runtime."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from skaal import APIGateway, App, AuthConfig, Route, TelemetryConfig
from skaal.backends.file_blob_backend import FileBlobBackend
from skaal.backends.local_backend import LocalMap
from skaal.blob import BlobStore
from skaal.runtime.local import LocalRuntime
from skaal.runtime.telemetry import RuntimeTelemetry
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


def _make_secured_counter_app(
    *,
    required: bool = True,
    issuer: str = "https://issuer.example.test",
    captured: dict[str, str | None] | None = None,
) -> App:
    app = _make_counter_app()
    app.attach(
        APIGateway(
            "public-api",
            routes=[Route("/counts/*", target="test-counter.increment")],
            auth=AuthConfig(
                provider="jwt",
                issuer=issuer,
                audience="skaal-tests",
                header="Authorization",
                required=required,
            ),
        )
    )

    if captured is not None:

        @app.add_before_invoke
        async def _capture(ctx: object) -> None:
            captured["auth_subject"] = getattr(ctx, "auth_subject", None)
            captured["trace_id"] = getattr(ctx, "trace_id", None)
            captured["span_id"] = getattr(ctx, "span_id", None)

    return app


def _make_jwt_client(issuer: str) -> tuple[str, httpx.AsyncClient]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    jwk = json.loads(RSAAlgorithm.to_jwk(public_key))
    jwk["kid"] = "test-key"
    token = jwt.encode(
        {
            "sub": "user-123",
            "aud": "skaal-tests",
            "iss": issuer,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == issuer.rstrip("/") + "/.well-known/jwks.json"
        return httpx.Response(200, json={"keys": [jwk]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    return token, client


def _metric_values(metrics_data: object, metric_name: str) -> list[int | float]:
    values: list[int | float] = []
    for resource_metric in getattr(metrics_data, "resource_metrics", []):
        for scope_metric in getattr(resource_metric, "scope_metrics", []):
            for metric in getattr(scope_metric, "metrics", []):
                if getattr(metric, "name", None) != metric_name:
                    continue
                for point in getattr(metric.data, "data_points", []):
                    values.append(point.value)
    return values


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
async def test_runtime_ready_distinguishes_startup_from_liveness():
    runtime = LocalRuntime(_make_counter_app())

    data, status = await runtime._dispatch("GET", "/ready", b"")
    assert status == 503
    assert data["status"] == "starting"

    await runtime.ensure_started()

    data, status = await runtime._dispatch("GET", "/ready", b"")
    assert status == 200
    assert data["status"] == "ready"

    await runtime.shutdown()


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
async def test_runtime_rejects_missing_jwt_when_required():
    issuer = "https://issuer.example.test"
    _, client = _make_jwt_client(issuer)
    runtime = LocalRuntime(_make_secured_counter_app(issuer=issuer), auth_http_client=client)

    data, status = await runtime._dispatch(
        "POST",
        _invoke_path(runtime.app, "increment"),
        json.dumps({"name": "hits"}).encode(),
        headers={},
    )

    assert status == 401
    assert "Missing Authorization header" in data["error"]
    await client.aclose()
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_runtime_optional_jwt_allows_missing_token():
    issuer = "https://issuer.example.test"
    _, client = _make_jwt_client(issuer)
    runtime = LocalRuntime(
        _make_secured_counter_app(required=False, issuer=issuer),
        auth_http_client=client,
    )

    data, status = await runtime._dispatch(
        "POST",
        _invoke_path(runtime.app, "increment"),
        json.dumps({"name": "hits"}).encode(),
        headers={},
    )

    assert status == 200
    assert data["value"] == 1
    await client.aclose()
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_runtime_valid_jwt_populates_invoke_context_trace_and_subject():
    captured: dict[str, str | None] = {}
    issuer = "https://issuer.example.test"
    token, client = _make_jwt_client(issuer)
    telemetry_config: TelemetryConfig = {
        "exporter": "otlp",
        "service_name": "test-counter",
        "service_namespace": "skaal-tests",
    }
    telemetry = RuntimeTelemetry(
        "test-counter",
        telemetry_config,
        span_exporter=InMemorySpanExporter(),
        metric_reader=InMemoryMetricReader(),
    )
    runtime = LocalRuntime(
        _make_secured_counter_app(issuer=issuer, captured=captured),
        auth_http_client=client,
        telemetry=telemetry_config,
        telemetry_runtime=telemetry,
    )

    data, status = await runtime._dispatch(
        "POST",
        _invoke_path(runtime.app, "increment"),
        json.dumps({"name": "hits"}).encode(),
        headers={"Authorization": f"Bearer {token}"},
    )

    assert status == 200
    assert data["value"] == 1
    assert captured["auth_subject"] == "user-123"
    assert captured["trace_id"] is not None
    assert captured["span_id"] is not None

    await client.aclose()
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_runtime_telemetry_emits_spans_and_metrics():
    span_exporter = InMemorySpanExporter()
    metric_reader = InMemoryMetricReader()
    telemetry_config: TelemetryConfig = {
        "exporter": "otlp",
        "service_name": "test-counter",
        "service_namespace": "skaal-tests",
    }
    telemetry = RuntimeTelemetry(
        "test-counter",
        telemetry_config,
        span_exporter=span_exporter,
        metric_reader=metric_reader,
    )
    runtime = LocalRuntime(
        _make_counter_app(),
        telemetry=telemetry_config,
        telemetry_runtime=telemetry,
    )

    _, status = await runtime._dispatch(
        "POST",
        _invoke_path(runtime.app, "increment"),
        json.dumps({"name": "hits"}).encode(),
    )

    assert status == 200
    spans = span_exporter.get_finished_spans()
    assert any(span.name == f"POST {_invoke_path(runtime.app, 'increment')}" for span in spans)

    metrics_data = metric_reader.get_metrics_data()
    assert any(value >= 1 for value in _metric_values(metrics_data, "skaal.http.requests"))

    await runtime.shutdown()


def test_typed_auth_and_telemetry_shapes_are_exported() -> None:
    auth: dict[str, object] = {
        "provider": "jwt",
        "issuer": "https://issuer.example.test",
        "audience": "skaal-tests",
        "header": "X-Auth",
        "required": False,
    }
    telemetry: TelemetryConfig = {
        "exporter": "otlp",
        "endpoint": "http://localhost:4318",
        "service_name": "demo",
        "service_namespace": "skaal",
        "headers": {"x-test": "1"},
        "insecure": True,
    }

    assert auth["header"] == "X-Auth"
    assert telemetry["exporter"] == "otlp"


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
