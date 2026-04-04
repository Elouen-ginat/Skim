"""Additional tests for LocalRuntime: factory methods and channel wiring."""

from __future__ import annotations

import pytest

from skaal import App
from skaal.runtime.local import LocalRuntime


@pytest.fixture
def counter_app() -> App:
    app = App("runtime-extras")

    @app.storage(read_latency="< 10ms", durability="ephemeral")
    class Counts:
        pass

    @app.function()
    async def increment(name: str, by: int = 1) -> dict:
        current = await Counts.get(name) or 0
        new = current + by
        await Counts.set(name, new)
        return {"name": name, "value": new}

    @app.function()
    async def get_count(name: str) -> dict:
        return {"name": name, "value": await Counts.get(name) or 0}

    return app


def test_runtime_default_in_memory(counter_app):
    """Default LocalRuntime uses in-memory LocalMap."""
    from skaal.backends.local_backend import LocalMap

    rt = LocalRuntime(counter_app)
    backends = list(rt._backends.values())
    assert all(isinstance(b, LocalMap) for b in backends)


def test_runtime_from_sqlite(counter_app, tmp_path):
    """from_sqlite creates a runtime with SqliteBackend instances."""
    from skaal.backends.sqlite_backend import SqliteBackend

    db = tmp_path / "test.db"
    rt = LocalRuntime.from_sqlite(counter_app, db_path=str(db))
    backends = list(rt._backends.values())
    assert all(isinstance(b, SqliteBackend) for b in backends)


def test_runtime_backend_override(counter_app):
    """Explicit backend_overrides replace default LocalMap."""
    from skaal.backends.local_backend import LocalMap

    custom = LocalMap()
    custom._data["seed"] = 42
    rt = LocalRuntime(counter_app, backend_overrides={"runtime-extras.Counts": custom})
    assert rt._backends["runtime-extras.Counts"] is custom


@pytest.mark.asyncio
async def test_runtime_dispatch_get(counter_app):
    """GET / returns app index."""
    rt = LocalRuntime(counter_app)
    result, status = await rt._dispatch("GET", "/", b"")
    assert status == 200
    assert "app" in result
    assert "endpoints" in result


@pytest.mark.asyncio
async def test_runtime_dispatch_increment(counter_app):
    """POST /increment increments the counter."""
    import json

    rt = LocalRuntime(counter_app)
    body = json.dumps({"name": "hits"}).encode()
    result, status = await rt._dispatch("POST", "/increment", body)
    assert status == 200
    assert result["value"] == 1

    result2, _ = await rt._dispatch("POST", "/increment", body)
    assert result2["value"] == 2


@pytest.mark.asyncio
async def test_runtime_dispatch_missing_function(counter_app):
    """POST to unknown function returns 404."""
    rt = LocalRuntime(counter_app)
    result, status = await rt._dispatch("POST", "/nonexistent", b"{}")
    assert status == 404


@pytest.mark.asyncio
async def test_runtime_dispatch_health(counter_app):
    """GET /health returns ok."""
    rt = LocalRuntime(counter_app)
    result, status = await rt._dispatch("GET", "/health", b"")
    assert status == 200
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_runtime_dispatch_bad_method(counter_app):
    """DELETE returns 405."""
    rt = LocalRuntime(counter_app)
    result, status = await rt._dispatch("DELETE", "/increment", b"")
    assert status == 405


def test_from_postgres_creates_backends(counter_app):
    """from_postgres() creates PostgresBackend instances (lazy connect)."""
    from skaal.backends.postgres_backend import PostgresBackend

    rt = LocalRuntime.from_postgres(counter_app, dsn="postgresql://user:pass@localhost/test")
    backends = list(rt._backends.values())
    assert all(isinstance(b, PostgresBackend) for b in backends)
    # Connections are lazy — no actual DB needed for this test
    assert all(b.dsn == "postgresql://user:pass@localhost/test" for b in backends)
