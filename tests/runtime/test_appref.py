"""Tests for AppRef — cross-app function call component."""

from __future__ import annotations

import pytest

from skaal import Secret
from skaal.components import AppRef

# ── _resolve_base_url ──────────────────────────────────────────────────────────


def test_resolve_base_url_literal():
    ref = AppRef("svc", base_url="http://localhost:8001")
    assert ref._resolve_base_url() == "http://localhost:8001"


def test_resolve_base_url_strips_trailing_slash():
    ref = AppRef("svc", base_url="http://localhost:8001/")
    assert ref._resolve_base_url() == "http://localhost:8001"


def test_resolve_base_url_env_var(monkeypatch):
    monkeypatch.setenv("PAYMENTS_URL", "https://payments.internal")
    ref = AppRef("svc", base_url_secret=Secret("PAYMENTS_URL"))
    assert ref._resolve_base_url() == "https://payments.internal"


def test_resolve_base_url_env_var_strips_slash(monkeypatch):
    monkeypatch.setenv("SVC_URL", "https://svc.internal/")
    ref = AppRef("svc", base_url_secret=Secret("SVC_URL"))
    assert ref._resolve_base_url() == "https://svc.internal"


def test_resolve_base_url_raises_when_neither_set(monkeypatch):
    monkeypatch.delenv("MISSING_URL", raising=False)
    ref = AppRef("svc", base_url_secret=Secret("MISSING_URL"))
    with pytest.raises(RuntimeError, match="MISSING_URL"):
        ref._resolve_base_url()


def test_resolve_base_url_raises_no_config():
    ref = AppRef("svc")
    with pytest.raises(RuntimeError):
        ref._resolve_base_url()


# ── __skaal_component__ metadata ────────────────────────────────────────────────


def test_appref_component_metadata():
    ref = AppRef("payments", base_url="http://pay:8000", timeout_ms=5_000)
    meta = ref.__skaal_component__
    assert meta["kind"] == "app-ref"
    assert meta["name"] == "payments"
    assert meta["timeout_ms"] == 5_000


def test_appref_component_kind():
    ref = AppRef("svc", base_url="http://svc:8000")
    assert ref._skaal_component_kind == "app-ref"


# ── call() via httpx mock ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_call_posts_to_correct_url(httpx_mock):
    httpx_mock.add_response(json={"result": 42})

    ref = AppRef("svc", base_url="http://svc:8001")
    result = await ref.call("compute", x=1, y=2)

    assert result == {"result": 42}
    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    assert str(requests[0].url) == "http://svc:8001/compute"
    assert requests[0].method == "POST"


@pytest.mark.asyncio
async def test_call_sends_json_body(httpx_mock):
    import json

    httpx_mock.add_response(json={"ok": True})

    ref = AppRef("svc", base_url="http://svc:8001")
    await ref.call("process", data="hello", count=3)

    request = httpx_mock.get_requests()[0]
    body = json.loads(request.content)
    assert body == {"data": "hello", "count": 3}


@pytest.mark.asyncio
async def test_call_raises_on_4xx(httpx_mock):
    httpx_mock.add_response(status_code=404, json={"error": "not found"})

    ref = AppRef("svc", base_url="http://svc:8001")
    with pytest.raises(RuntimeError, match="404"):
        await ref.call("missing")


@pytest.mark.asyncio
async def test_call_raises_on_5xx(httpx_mock):
    httpx_mock.add_response(status_code=500, json={"error": "server error"})

    ref = AppRef("svc", base_url="http://svc:8001")
    with pytest.raises(RuntimeError, match="500"):
        await ref.call("broken")


@pytest.mark.asyncio
async def test_call_includes_error_detail_in_exception(httpx_mock):
    httpx_mock.add_response(status_code=422, json={"error": "bad input", "field": "amount"})

    ref = AppRef("svc", base_url="http://svc:8001")
    with pytest.raises(RuntimeError, match="bad input"):
        await ref.call("validate", amount=-1)


# ── __getattr__ proxy ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_proxy_attribute_delegates_to_call(httpx_mock):
    httpx_mock.add_response(json={"charged": True})

    ref = AppRef("payments", base_url="http://pay:8001")
    result = await ref.charge(amount=100, currency="USD")

    assert result == {"charged": True}
    request = httpx_mock.get_requests()[0]
    assert str(request.url) == "http://pay:8001/charge"


@pytest.mark.asyncio
async def test_proxy_uses_env_var_url(httpx_mock, monkeypatch):
    monkeypatch.setenv("PAY_URL", "http://pay:9000")
    httpx_mock.add_response(json={"ok": True})

    ref = AppRef("payments", base_url_secret=Secret("PAY_URL"))
    await ref.refund(order_id="abc")

    request = httpx_mock.get_requests()[0]
    assert str(request.url) == "http://pay:9000/refund"


def test_proxy_dunder_attribute_raises_attribute_error():
    ref = AppRef("svc", base_url="http://svc:8001")
    with pytest.raises(AttributeError):
        _ = ref.__nonexistent_dunder__
