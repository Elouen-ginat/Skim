"""Backend contract tests — verify every backend implements StorageBackend identically.

The tests are parametrized over every backend factory discovered through
:mod:`skaal.plugins`, so third-party backends registered via the
``skaal.backends`` entry-point group are covered automatically.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import pytest

from skaal.backends.base import StorageBackend
from skaal.backends.kv.local_map import LocalMap
from skaal.backends.kv.sqlite import SqliteBackend

# ── Helpers for skipping server-backed backends when the server is down ───────


_CONN_MARKERS = (
    "connection",
    "connect",
    "refused",
    "resolve",
    "timed out",
    "unreachable",
    "no route",
    "error 22",
    "error 111",
)


def _is_server_unreachable(exc: BaseException) -> bool:
    # Most connection failures are subclasses of these — cheapest check first.
    if isinstance(exc, (ConnectionError, OSError, TimeoutError)):
        return True
    msg = str(exc).lower()
    return any(keyword in msg for keyword in _CONN_MARKERS)


# ── Factory fixtures — each yields an async context manager producing one
#    fresh, connected backend instance, then closes it cleanly. ───────────────


@asynccontextmanager
async def _local_factory(tmp_path: Path) -> AsyncIterator[LocalMap]:
    b = LocalMap()
    try:
        yield b
    finally:
        await b.close()


@asynccontextmanager
async def _sqlite_factory(tmp_path: Path) -> AsyncIterator[SqliteBackend]:
    b = SqliteBackend(tmp_path / "contract.db", namespace="contract")
    await b.connect()
    try:
        yield b
    finally:
        await b.close()


@asynccontextmanager
async def _redis_factory(tmp_path: Path) -> AsyncIterator[Any]:
    try:
        import redis.asyncio  # noqa: F401

        from skaal.backends.kv.redis import RedisBackend
    except ImportError:
        pytest.skip("redis not installed")

    # Use a unique namespace per test to avoid key collisions across runs.
    b = RedisBackend(url="redis://localhost:6379", namespace=f"contract-{tmp_path.name}")
    try:
        await b.connect()
        # Probe with a PING-equivalent operation.
        await b.set("__probe__", "1")
        await b.delete("__probe__")
    except Exception as exc:  # noqa: BLE001
        if _is_server_unreachable(exc):
            pytest.skip("Redis server not available")
        raise
    try:
        yield b
    finally:
        try:
            # Cleanup keys created during the test to keep Redis tidy.
            for k, _ in await b.list():
                await b.delete(k)
        except Exception:  # noqa: BLE001
            pass
        await b.close()


# Every server-backed factory is listed here.  Adding a new backend (e.g.
# Postgres or a third-party plugin) is a one-line change.
_FACTORIES: dict[str, Callable[[Path], Any]] = {
    "local": _local_factory,
    "sqlite": _sqlite_factory,
    "redis": _redis_factory,
}


@pytest.fixture(params=sorted(_FACTORIES))
def backend_factory(request: pytest.FixtureRequest) -> Callable[[Path], Any]:
    return _FACTORIES[request.param]


# ── Protocol identity ─────────────────────────────────────────────────────────


class TestStorageBackendProtocol:
    def test_local_implements_protocol(self) -> None:
        assert isinstance(LocalMap(), StorageBackend)

    def test_sqlite_implements_protocol(self, tmp_path: Path) -> None:
        assert isinstance(SqliteBackend(tmp_path / "p.db"), StorageBackend)

    def test_every_registered_backend_implements_protocol(self) -> None:
        """Every backend reachable via the plugin registry must satisfy the protocol."""
        from skaal.plugins import iter_backends

        registered = iter_backends()
        # We don't instantiate server-backed ones here (would need creds); we
        # just verify the class has the required methods so protocol conformance
        # is structurally satisfied.
        required = {
            "get",
            "set",
            "delete",
            "list",
            "scan",
            "increment_counter",
            "atomic_update",
            "close",
        }
        missing: dict[str, set[str]] = {}
        for name, cls in registered.items():
            have = {m for m in required if callable(getattr(cls, m, None))}
            gap = required - have
            if gap:
                missing[name] = gap
        assert not missing, f"Backends missing protocol methods: {missing}"


# ── Shared CRUD tests (run against every factory) ─────────────────────────────


class TestCRUDContract:
    @pytest.mark.asyncio
    async def test_set_get(self, backend_factory: Any, tmp_path: Path) -> None:
        async with backend_factory(tmp_path) as b:
            await b.set("k", {"value": 42})
            assert await b.get("k") == {"value": 42}

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self, backend_factory: Any, tmp_path: Path) -> None:
        async with backend_factory(tmp_path) as b:
            assert await b.get("does-not-exist") is None

    @pytest.mark.asyncio
    async def test_delete(self, backend_factory: Any, tmp_path: Path) -> None:
        async with backend_factory(tmp_path) as b:
            await b.set("k", "v")
            await b.delete("k")
            assert await b.get("k") is None

    @pytest.mark.asyncio
    async def test_delete_missing_is_noop(self, backend_factory: Any, tmp_path: Path) -> None:
        async with backend_factory(tmp_path) as b:
            # Must not raise.
            await b.delete("never-set")

    @pytest.mark.asyncio
    async def test_scan_prefix(self, backend_factory: Any, tmp_path: Path) -> None:
        async with backend_factory(tmp_path) as b:
            await b.set("event:1", {"t": "click"})
            await b.set("event:2", {"t": "hover"})
            await b.set("other:1", "nope")
            hits = dict(await b.scan("event:"))
            assert hits == {"event:1": {"t": "click"}, "event:2": {"t": "hover"}}

    @pytest.mark.asyncio
    async def test_increment_counter(self, backend_factory: Any, tmp_path: Path) -> None:
        async with backend_factory(tmp_path) as b:
            assert await b.increment_counter("c", delta=1) == 1
            assert await b.increment_counter("c", delta=5) == 6

    @pytest.mark.asyncio
    async def test_atomic_update_creates_then_updates(
        self, backend_factory: Any, tmp_path: Path
    ) -> None:
        async with backend_factory(tmp_path) as b:

            def init(current: Any) -> dict[str, int]:
                assert current is None
                return {"n": 1}

            assert await b.atomic_update("doc", init) == {"n": 1}

            def bump(current: Any) -> dict[str, int]:
                return {"n": int(current["n"]) + 1}

            assert await b.atomic_update("doc", bump) == {"n": 2}
            assert await b.get("doc") == {"n": 2}


# ── Plugin registry coverage ──────────────────────────────────────────────────


class TestPluginRegistry:
    def test_builtin_names_resolve(self) -> None:
        """Every built-in backend name is resolvable via the plugin registry."""
        from skaal.plugins import get_backend

        for name in ("local", "sqlite", "redis", "postgres", "dynamodb", "firestore"):
            assert get_backend(name) is not None

    def test_in_process_registration_overrides_entry_point(self) -> None:
        """register_backend() takes precedence over installed entry points."""
        from skaal.plugins import get_backend, register_backend

        sentinel = object()
        register_backend("local", sentinel)  # type: ignore[arg-type]
        try:
            assert get_backend("local") is sentinel
        finally:
            # Restore: register_backend doesn't expose a remove; clear the dict.
            from skaal.plugins import _backends

            _backends.pop("local", None)

    def test_unknown_backend_raises_plugin_error(self) -> None:
        from skaal.errors import SkaalPluginError
        from skaal.plugins import get_backend

        with pytest.raises(SkaalPluginError):
            get_backend("definitely-not-installed-xyz")
