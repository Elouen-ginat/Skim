"""SQLite-backed persistent key-value store via aiosqlite."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, List, cast

from skaal.storage import (
    _cursor_identity,
    _encode_cursor,
    _get_backend_indexes,
    _normalize_limit,
    _validate_cursor,
)
from skaal.types.storage import Page


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
        self._db: Any = None  # aiosqlite connection, lazy-opened
        self._engine: Any = None
        self._session_factory: Any = None

    def _sqlalchemy_url(self) -> str:
        raw = str(self.path)
        if raw == ":memory:":
            return "sqlite+aiosqlite:///:memory:"
        return f"sqlite+aiosqlite:///{self.path.resolve().as_posix()}"

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

    async def _ensure_relational_engine(self) -> None:
        if self._engine is not None:
            return

        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlmodel.ext.asyncio.session import AsyncSession

        self._engine = create_async_engine(self._sqlalchemy_url())
        self._session_factory = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

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

    async def list_page(self, *, limit: int, cursor: str | None):
        await self._ensure_connected()
        limit = _normalize_limit(limit)
        decoded = _validate_cursor(cursor, mode="list")
        last_key = decoded.get("last_key")
        query = "SELECT key, value FROM kv WHERE ns = ?"
        params: list[Any] = [self.namespace]
        if last_key is not None:
            query += " AND key > ?"
            params.append(last_key)
        query += " ORDER BY key LIMIT ?"
        params.append(limit + 1)
        async with self._db.execute(query, tuple(params)) as sql_cursor:
            rows = await sql_cursor.fetchall()
        page_rows = rows[:limit]
        has_more = len(rows) > limit
        items = [(row[0], json.loads(row[1])) for row in page_rows]
        next_cursor = None
        if has_more and page_rows:
            next_cursor = _encode_cursor({"mode": "list", "last_key": page_rows[-1][0]})
        return Page(items=items, next_cursor=next_cursor, has_more=has_more)

    async def scan(self, prefix: str = "") -> List[tuple[str, Any]]:
        await self._ensure_connected()
        # Escape LIKE wildcards to prevent injection: % and _ are special in LIKE
        escaped_prefix = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        async with self._db.execute(
            "SELECT key, value FROM kv WHERE ns = ? AND key LIKE ? ESCAPE '\\'",
            (self.namespace, f"{escaped_prefix}%"),
        ) as cursor:
            rows = await cursor.fetchall()
        return [(row[0], json.loads(row[1])) for row in rows]

    async def scan_page(self, prefix: str = "", *, limit: int, cursor: str | None):
        await self._ensure_connected()
        limit = _normalize_limit(limit)
        decoded = _validate_cursor(cursor, mode="scan", extra={"prefix": prefix})
        last_key = decoded.get("last_key")
        escaped_prefix = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = "SELECT key, value FROM kv WHERE ns = ? AND key LIKE ? ESCAPE '\\'"
        params: list[Any] = [self.namespace, f"{escaped_prefix}%"]
        if last_key is not None:
            query += " AND key > ?"
            params.append(last_key)
        query += " ORDER BY key LIMIT ?"
        params.append(limit + 1)
        async with self._db.execute(query, tuple(params)) as sql_cursor:
            rows = await sql_cursor.fetchall()
        page_rows = rows[:limit]
        has_more = len(rows) > limit
        items = [(row[0], json.loads(row[1])) for row in page_rows]
        next_cursor = None
        if has_more and page_rows:
            next_cursor = _encode_cursor(
                {"mode": "scan", "prefix": prefix, "last_key": page_rows[-1][0]}
            )
        return Page(items=items, next_cursor=next_cursor, has_more=has_more)

    async def query_index(
        self,
        index_name: str,
        key: Any,
        *,
        limit: int,
        cursor: str | None,
    ):
        await self._ensure_connected()
        limit = _normalize_limit(limit)
        indexes = _get_backend_indexes(self)
        index = indexes.get(index_name)
        if index is None:
            raise ValueError(f"No secondary index named {index_name!r}")

        decoded = _validate_cursor(
            cursor,
            mode="index",
            extra={"index_name": index_name, "key": _cursor_identity(key)},
        )
        partition_path = f"$.{index.partition_key}"

        if index.sort_key is None:
            query = "SELECT key, value FROM kv WHERE ns = ? AND json_extract(value, ?) = ?"
            params: list[Any] = [self.namespace, partition_path, key]
            last_key = decoded.get("last_key")
            if last_key is not None:
                query += " AND key > ?"
                params.append(last_key)
            query += " ORDER BY key LIMIT ?"
            params.append(limit + 1)
            async with self._db.execute(query, tuple(params)) as sql_cursor:
                rows = await sql_cursor.fetchall()
            page_rows = rows[:limit]
            has_more = len(rows) > limit
            items = [json.loads(row[1]) for row in page_rows]
            next_cursor = None
            if has_more and page_rows:
                next_cursor = _encode_cursor(
                    {
                        "mode": "index",
                        "index_name": index_name,
                        "key": _cursor_identity(key),
                        "last_key": page_rows[-1][0],
                    }
                )
            return Page(items=items, next_cursor=next_cursor, has_more=has_more)

        sort_path = f"$.{index.sort_key}"
        query = (
            "SELECT key, value, json_extract(value, ?) AS sort_value "
            "FROM kv WHERE ns = ? AND json_extract(value, ?) = ?"
        )
        params = [sort_path, self.namespace, partition_path, key]
        if decoded.get("has_last_sort"):
            last_sort = decoded.get("last_sort")
            last_key = decoded.get("last_key")
            if last_sort is None:
                query += (
                    " AND (json_extract(value, ?) IS NOT NULL "
                    "OR (json_extract(value, ?) IS NULL AND key > ?))"
                )
                params.extend([sort_path, sort_path, last_key])
            else:
                query += (
                    " AND (json_extract(value, ?) > ? OR (json_extract(value, ?) = ? AND key > ?))"
                )
                params.extend([sort_path, last_sort, sort_path, last_sort, last_key])
        query += " ORDER BY sort_value, key LIMIT ?"
        params.append(limit + 1)
        async with self._db.execute(query, tuple(params)) as sql_cursor:
            rows = await sql_cursor.fetchall()
        page_rows = rows[:limit]
        has_more = len(rows) > limit
        items = [json.loads(row[1]) for row in page_rows]
        next_cursor = None
        if has_more and page_rows:
            next_cursor = _encode_cursor(
                {
                    "mode": "index",
                    "index_name": index_name,
                    "key": _cursor_identity(key),
                    "has_last_sort": True,
                    "last_sort": page_rows[-1][2],
                    "last_key": page_rows[-1][0],
                }
            )
        return Page(items=items, next_cursor=next_cursor, has_more=has_more)

    async def increment_counter(self, key: str, delta: int = 1) -> int:
        """Atomically increment a counter using a transaction."""
        await self._ensure_connected()
        # Start a transaction to ensure atomicity
        await self._db.execute("BEGIN IMMEDIATE")
        try:
            async with self._db.execute(
                "SELECT value FROM kv WHERE ns = ? AND key = ?",
                (self.namespace, key),
            ) as cursor:
                row = await cursor.fetchone()

            current = int(json.loads(row[0])) if row else 0
            new_value = current + delta

            await self._db.execute(
                """
                INSERT INTO kv (ns, key, value) VALUES (?, ?, ?)
                ON CONFLICT (ns, key) DO UPDATE SET value = excluded.value
                """,
                (self.namespace, key, json.dumps(new_value)),
            )
            await self._db.commit()
            return new_value
        except Exception:
            await self._db.rollback()
            raise

    async def atomic_update(self, key: str, fn: Any) -> Any:
        """Atomically read, apply fn, write back, and return the result.

        Uses BEGIN IMMEDIATE to prevent concurrent writers — safe across
        multiple gunicorn worker processes sharing the same SQLite file.
        """
        await self._ensure_connected()
        await self._db.execute("BEGIN IMMEDIATE")
        try:
            async with self._db.execute(
                "SELECT value FROM kv WHERE ns = ? AND key = ?",
                (self.namespace, key),
            ) as cursor:
                row = await cursor.fetchone()
            current = json.loads(row[0]) if row else None
            updated = fn(current)
            await self._db.execute(
                """
                INSERT INTO kv (ns, key, value) VALUES (?, ?, ?)
                ON CONFLICT (ns, key) DO UPDATE SET value = excluded.value
                """,
                (self.namespace, key, json.dumps(updated)),
            )
            await self._db.commit()
            return updated
        except Exception:
            await self._db.rollback()
            raise

    async def ensure_relational_schema(self, model_cls: type) -> None:
        """Create missing SQLModel tables for *model_cls*."""
        await self._ensure_relational_engine()
        typed_model = cast(Any, model_cls)
        async with self._engine.begin() as conn:
            await conn.run_sync(typed_model.metadata.create_all)

    async def relational_engine(self) -> Any:
        """Return the SQLAlchemy ``AsyncEngine`` used for the relational tier."""
        await self._ensure_relational_engine()
        return self._engine

    @asynccontextmanager
    async def open_relational_session(self, model_cls: type) -> AsyncIterator[Any]:
        """Yield an AsyncSession bound to this backend's SQLite engine."""
        await self.ensure_relational_schema(model_cls)
        assert self._session_factory is not None
        async with self._session_factory() as session:
            yield session

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None

    def __repr__(self) -> str:
        return f"SqliteBackend(path={self.path!r}, namespace={self.namespace!r})"
