"""SQLite-backed persistent key-value store via aiosqlite."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class SqliteBackend:
    """
    Persistent KV store backed by SQLite. Zero server setup.

    Schema:
        CREATE TABLE IF NOT EXISTS kv (
            key   TEXT NOT NULL,
            ns    TEXT NOT NULL DEFAULT '',
            value TEXT NOT NULL,
            PRIMARY KEY (ns, key)
        )

    The `namespace` param namespaces all keys so multiple storage classes
    can share one SQLite file.

    Usage:
        backend = SqliteBackend("skaal_local.db", namespace="Counts")
        await backend.connect()
        await backend.set("hits", 42)
        val = await backend.get("hits")   # 42
        await backend.close()
    """

    def __init__(
        self,
        path: str | Path = "skaal_local.db",
        namespace: str = "default",
    ) -> None:
        self.path = Path(path)
        self.namespace = namespace
        self._db = None  # aiosqlite connection, lazy-opened

    async def connect(self) -> None:
        """Open the SQLite connection and create table if needed."""
        import aiosqlite

        self._db = await aiosqlite.connect(self.path)
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS kv (
                key   TEXT NOT NULL,
                ns    TEXT NOT NULL DEFAULT '',
                value TEXT NOT NULL,
                PRIMARY KEY (ns, key)
            )
            """
        )
        await self._db.commit()

    async def _ensure_connected(self) -> None:
        if self._db is None:
            await self.connect()

    async def get(self, key: str) -> Any | None:
        await self._ensure_connected()
        async with self._db.execute(
            "SELECT value FROM kv WHERE ns = ? AND key = ?",
            (self.namespace, key),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    async def set(self, key: str, value: Any) -> None:
        await self._ensure_connected()
        await self._db.execute(
            """
            INSERT INTO kv (ns, key, value) VALUES (?, ?, ?)
            ON CONFLICT (ns, key) DO UPDATE SET value = excluded.value
            """,
            (self.namespace, key, json.dumps(value)),
        )
        await self._db.commit()

    async def delete(self, key: str) -> None:
        await self._ensure_connected()
        await self._db.execute(
            "DELETE FROM kv WHERE ns = ? AND key = ?",
            (self.namespace, key),
        )
        await self._db.commit()

    async def list(self) -> list[tuple[str, Any]]:
        await self._ensure_connected()
        async with self._db.execute(
            "SELECT key, value FROM kv WHERE ns = ?",
            (self.namespace,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [(row[0], json.loads(row[1])) for row in rows]

    async def scan(self, prefix: str = "") -> list[tuple[str, Any]]:
        await self._ensure_connected()
        async with self._db.execute(
            "SELECT key, value FROM kv WHERE ns = ? AND key LIKE ?",
            (self.namespace, f"{prefix}%"),
        ) as cursor:
            rows = await cursor.fetchall()
        return [(row[0], json.loads(row[1])) for row in rows]

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    def __repr__(self) -> str:
        return f"SqliteBackend(path={self.path!r}, namespace={self.namespace!r})"
