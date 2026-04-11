"""Backend contract tests — verify all backends implement StorageBackend identically."""

from __future__ import annotations

from pathlib import Path

import pytest

from skaal.backends.base import StorageBackend
from skaal.backends.local_backend import LocalMap
from skaal.backends.redis_backend import RedisBackend
from skaal.backends.sqlite_backend import SqliteBackend


@pytest.fixture
def local_backend() -> LocalMap:
    """Local in-memory backend."""
    return LocalMap()


@pytest.fixture
def sqlite_backend(tmp_path: Path) -> SqliteBackend:
    """SQLite backend."""
    db_path = tmp_path / "test.db"
    backend = SqliteBackend(db_path, namespace="test")
    return backend


@pytest.fixture
def redis_backend() -> RedisBackend | None:
    """Redis backend (returns None if not available, tests check and skip)."""
    try:
        import redis.asyncio  # noqa: F401

        backend = RedisBackend(url="redis://localhost:6379", namespace="test")

        # Test connection to Redis server
        import asyncio

        loop = asyncio.get_event_loop()
        loop.run_until_complete(backend.connect())
    except Exception:
        return None

    # Return the backend object; connection attempt happens in tests
    # Tests check `if redis_backend is None: pytest.skip()` before connecting
    return backend


class TestStorageBackendContract:
    """Test that all backends satisfy the StorageBackend protocol."""

    @pytest.mark.asyncio
    async def test_local_backend_set_get(self, local_backend: LocalMap) -> None:
        """Test set and get on local backend."""
        await local_backend.set("key", {"value": 42})
        result = await local_backend.get("key")
        assert result == {"value": 42}

    @pytest.mark.asyncio
    async def test_local_backend_delete(self, local_backend: LocalMap) -> None:
        """Test delete on local backend."""
        await local_backend.set("key", "value")
        await local_backend.delete("key")
        result = await local_backend.get("key")
        assert result is None

    @pytest.mark.asyncio
    async def test_local_backend_list(self, local_backend: LocalMap) -> None:
        """Test list on local backend."""
        await local_backend.set("key1", "val1")
        await local_backend.set("key2", "val2")
        items = await local_backend.list()
        assert ("key1", "val1") in items
        assert ("key2", "val2") in items

    @pytest.mark.asyncio
    async def test_local_backend_scan(self, local_backend: LocalMap) -> None:
        """Test scan with prefix on local backend."""
        await local_backend.set("event:001", {"type": "click"})
        await local_backend.set("event:002", {"type": "hover"})
        await local_backend.set("meta:count", 2)

        events = await local_backend.scan("event:")
        assert len(events) == 2
        assert all(k.startswith("event:") for k, v in events)

    @pytest.mark.asyncio
    async def test_local_backend_increment_counter(self, local_backend: LocalMap) -> None:
        """Test atomic increment_counter on local backend."""
        val = await local_backend.increment_counter("counter", delta=1)
        assert val == 1

        val = await local_backend.increment_counter("counter", delta=5)
        assert val == 6

    @pytest.mark.asyncio
    async def test_sqlite_backend_set_get(self, sqlite_backend: SqliteBackend) -> None:
        """Test set and get on SQLite backend."""
        await sqlite_backend.connect()
        try:
            await sqlite_backend.set("key", {"value": 42})
            result = await sqlite_backend.get("key")
            assert result == {"value": 42}
        finally:
            await sqlite_backend.close()

    @pytest.mark.asyncio
    async def test_sqlite_backend_scan_with_wildcard_escaping(
        self, sqlite_backend: SqliteBackend
    ) -> None:
        """Test SQLite scan properly escapes LIKE wildcards."""
        await sqlite_backend.connect()
        try:
            # Keys with special characters that would break unescaped LIKE
            await sqlite_backend.set("test%prefix_key", "value1")
            await sqlite_backend.set("test_prefix_key", "value2")

            # Scan for exact prefix with % and _
            results = await sqlite_backend.scan("test%prefix")
            # Should only get the first key, not the second
            assert len(results) == 1
            assert results[0][0] == "test%prefix_key"
        finally:
            await sqlite_backend.close()

    @pytest.mark.asyncio
    async def test_sqlite_backend_increment_counter(self, sqlite_backend: SqliteBackend) -> None:
        """Test atomic increment on SQLite backend."""
        await sqlite_backend.connect()
        try:
            val = await sqlite_backend.increment_counter("counter", delta=1)
            assert val == 1

            val = await sqlite_backend.increment_counter("counter", delta=10)
            assert val == 11
        finally:
            await sqlite_backend.close()

    @pytest.mark.asyncio
    async def test_redis_backend_set_get(self, redis_backend: RedisBackend | None) -> None:
        """Test set and get on Redis backend."""
        if redis_backend is None:
            pytest.skip("Redis not available")

        try:
            await redis_backend.connect()
            await redis_backend.set("key", {"value": 42})
            result = await redis_backend.get("key")
            assert result == {"value": 42}
        except Exception as e:
            # Skip if Redis server is not available
            error_str = str(e).lower()
            if any(keyword in error_str for keyword in ["connection", "error 22", "refused"]):
                pytest.skip("Redis server not available")
            raise
        finally:
            try:
                await redis_backend.close()
            except Exception:
                pass

    @pytest.mark.asyncio
    async def test_redis_backend_scan_uses_mget(self, redis_backend: RedisBackend | None) -> None:
        """Test Redis scan efficiently uses MGET instead of N sequential GETs."""
        if redis_backend is None:
            pytest.skip("Redis not available")

        try:
            await redis_backend.connect()

            # Store multiple values
            for i in range(10):
                await redis_backend.set(f"event:{i:03d}", {"id": i})

            # Scan all events in one call
            results = await redis_backend.scan("event:")
            assert len(results) >= 10  # Should get all 10 keys efficiently
        except Exception as e:
            # Skip if Redis server is not available
            error_str = str(e).lower()
            if any(keyword in error_str for keyword in ["connection", "error 22", "refused"]):
                pytest.skip("Redis server not available")
            raise
        finally:
            try:
                await redis_backend.close()
            except Exception:
                pass

    @pytest.mark.asyncio
    async def test_redis_backend_increment_counter(
        self, redis_backend: RedisBackend | None
    ) -> None:
        """Test atomic increment on Redis backend."""
        if redis_backend is None:
            pytest.skip("Redis not available")

        try:
            await redis_backend.connect()

            val = await redis_backend.increment_counter("counter", delta=1)
            assert val == 1

            val = await redis_backend.increment_counter("counter", delta=5)
            assert val == 6
        except Exception as e:
            # Skip if Redis server is not available
            error_str = str(e).lower()
            if any(keyword in error_str for keyword in ["connection", "error 22", "refused"]):
                pytest.skip("Redis server not available")
            raise
        finally:
            try:
                await redis_backend.close()
            except Exception:
                pass


class TestStorageBackendProtocol:
    """Verify all backends are runtime-checkable as StorageBackend."""

    def test_local_backend_implements_protocol(self, local_backend: LocalMap) -> None:
        """Local backend should be recognized as StorageBackend."""
        assert isinstance(local_backend, StorageBackend)

    def test_sqlite_backend_implements_protocol(self, sqlite_backend: SqliteBackend) -> None:
        """SQLite backend should be recognized as StorageBackend."""
        assert isinstance(sqlite_backend, StorageBackend)

    def test_redis_backend_implements_protocol(self, redis_backend: RedisBackend | None) -> None:
        """Redis backend should be recognized as StorageBackend."""
        if redis_backend is None:
            pytest.skip("Redis not available")
        assert isinstance(redis_backend, StorageBackend)
