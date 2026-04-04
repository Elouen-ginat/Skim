"""PostgreSQL-backed KV store via asyncpg connection pool."""

from __future__ import annotations

import json
from typing import Any, List


class PostgresBackend:
    """
    KV store backed by PostgreSQL. Uses a connection pool via asyncpg.

    Table (auto-created on connect):
        CREATE TABLE IF NOT EXISTS skaal_kv (
            ns    TEXT    NOT NULL DEFAULT '',
            key   TEXT    NOT NULL,
            value JSONB   NOT NULL,
            PRIMARY KEY (ns, key)
        )

    Usage:
        backend = PostgresBackend("postgresql://user:pass@localhost/mydb", namespace="Counts")
        await backend.connect()
        await backend.set("key", {"score": 42})
        val = await backend.get("key")   # {"score": 42}
        await backend.close()
    """

    def __init__(
        self,
        dsn: str,
        namespace: str = "default",
        min_size: int = 1,
        max_size: int = 10,
    ) -> None:
        self.dsn = dsn
        self.namespace = namespace
        self.min_size = min_size
        self.max_size = max_size
        self._pool: Any = None  # asyncpg pool, lazy-created

    async def connect(self) -> None:
        """Create the asyncpg connection pool and ensure table exists."""
        import asyncpg

        self._pool = await asyncpg.create_pool(
            self.dsn,
            min_size=self.min_size,
            max_size=self.max_size,
        )
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS skaal_kv (
                    ns    TEXT    NOT NULL DEFAULT '',
                    key   TEXT    NOT NULL,
                    value JSONB   NOT NULL,
                    PRIMARY KEY (ns, key)
                )
                """
            )

    async def _ensure_connected(self) -> None:
        if self._pool is None:
            await self.connect()

    async def get(self, key: str) -> Any | None:
        await self._ensure_connected()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM skaal_kv WHERE ns = $1 AND key = $2",
                self.namespace,
                key,
            )
        if row is None:
            return None
        # asyncpg returns JSONB as a string; parse it
        raw = row["value"]
        if isinstance(raw, str):
            return json.loads(raw)
        return raw

    async def set(self, key: str, value: Any) -> None:
        await self._ensure_connected()
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO skaal_kv (ns, key, value)
                VALUES ($1, $2, $3::jsonb)
                ON CONFLICT (ns, key) DO UPDATE SET value = excluded.value
                """,
                self.namespace,
                key,
                json.dumps(value),
            )

    async def delete(self, key: str) -> None:
        await self._ensure_connected()
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM skaal_kv WHERE ns = $1 AND key = $2",
                self.namespace,
                key,
            )

    async def list(self) -> list[tuple[str, Any]]:
        await self._ensure_connected()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value FROM skaal_kv WHERE ns = $1",
                self.namespace,
            )
        result = []
        for row in rows:
            raw = row["value"]
            val = json.loads(raw) if isinstance(raw, str) else raw
            result.append((row["key"], val))
        return result

    async def scan(self, prefix: str = "") -> List[tuple[str, Any]]:
        await self._ensure_connected()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value FROM skaal_kv WHERE ns = $1 AND key LIKE $2",
                self.namespace,
                f"{prefix}%",
            )
        result = []
        for row in rows:
            raw = row["value"]
            val = json.loads(raw) if isinstance(raw, str) else raw
            result.append((row["key"], val))
        return result

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def __repr__(self) -> str:
        return f"PostgresBackend(dsn={self.dsn!r}, namespace={self.namespace!r})"
