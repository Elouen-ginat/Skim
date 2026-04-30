"""PostgreSQL-backed KV store via asyncpg connection pool."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, List, cast

from skaal.errors import SkaalConflict, SkaalUnavailable
from skaal.storage import (
    _cursor_identity,
    _decode_cursor,
    _encode_cursor,
    _get_backend_indexes,
    _normalize_limit,
)
from skaal.types.storage import Page


def _validate_cursor(
    cursor: str | None,
    *,
    mode: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    decoded = _decode_cursor(cursor)
    expected = {"mode": mode, **(extra or {})}
    for key, value in expected.items():
        if decoded and decoded.get(key) != value:
            raise ValueError("Cursor does not match this query")
    return decoded


def _decode_jsonb(raw: Any) -> Any:
    return json.loads(raw) if isinstance(raw, str) else raw


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
        self._pool_loop: asyncio.AbstractEventLoop | None = None  # loop that owns the pool
        self._engine: Any = None
        self._session_factory: Any = None

    def _sqlalchemy_dsn(self) -> str:
        if self.dsn.startswith("postgresql+asyncpg://"):
            return self.dsn
        if self.dsn.startswith("postgresql://"):
            return "postgresql+asyncpg://" + self.dsn[len("postgresql://") :]
        if self.dsn.startswith("postgres://"):
            return "postgresql+asyncpg://" + self.dsn[len("postgres://") :]
        return self.dsn

    async def connect(self) -> None:
        """Create the asyncpg connection pool and ensure table exists."""
        import asyncpg

        self._pool = await asyncpg.create_pool(
            self.dsn,
            min_size=self.min_size,
            max_size=self.max_size,
        )
        self._pool_loop = asyncio.get_running_loop()
        async with self._pool.acquire() as conn:
            try:
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
            except asyncpg.exceptions.UniqueViolationError:
                # Another worker created the table concurrently — safe to ignore.
                pass

    async def _ensure_connected(self) -> None:
        current_loop = asyncio.get_running_loop()
        if self._pool is not None and self._pool_loop is not current_loop:
            # asyncio.run() closes its event loop after each call, so the pool
            # bound to a previous loop is now stale. Discard it (can't await
            # close() — the old loop is already gone) and create a fresh one.
            self._pool = None
            self._pool_loop = None
        if self._pool is None:
            await self.connect()

    async def _ensure_relational_engine(self) -> None:
        if self._engine is not None:
            return

        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlmodel.ext.asyncio.session import AsyncSession

        self._engine = create_async_engine(self._sqlalchemy_dsn())
        self._session_factory = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

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
            val = _decode_jsonb(row["value"])
            result.append((row["key"], val))
        return result

    async def list_page(self, *, limit: int, cursor: str | None):
        await self._ensure_connected()
        limit = _normalize_limit(limit)
        decoded = _validate_cursor(cursor, mode="list")
        last_key = decoded.get("last_key")
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT key, value
                FROM skaal_kv
                WHERE ns = $1 AND ($2::text IS NULL OR key > $2)
                ORDER BY key
                LIMIT $3
                """,
                self.namespace,
                last_key,
                limit + 1,
            )
        page_rows = rows[:limit]
        has_more = len(rows) > limit
        items = [(row["key"], _decode_jsonb(row["value"])) for row in page_rows]
        next_cursor = None
        if has_more and page_rows:
            next_cursor = _encode_cursor({"mode": "list", "last_key": page_rows[-1]["key"]})
        return Page(items=items, next_cursor=next_cursor, has_more=has_more)

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
            val = _decode_jsonb(row["value"])
            result.append((row["key"], val))
        return result

    async def scan_page(self, prefix: str = "", *, limit: int, cursor: str | None):
        await self._ensure_connected()
        limit = _normalize_limit(limit)
        decoded = _validate_cursor(cursor, mode="scan", extra={"prefix": prefix})
        last_key = decoded.get("last_key")
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT key, value
                FROM skaal_kv
                WHERE ns = $1 AND key LIKE $2 AND ($3::text IS NULL OR key > $3)
                ORDER BY key
                LIMIT $4
                """,
                self.namespace,
                f"{prefix}%",
                last_key,
                limit + 1,
            )
        page_rows = rows[:limit]
        has_more = len(rows) > limit
        items = [(row["key"], _decode_jsonb(row["value"])) for row in page_rows]
        next_cursor = None
        if has_more and page_rows:
            next_cursor = _encode_cursor(
                {"mode": "scan", "prefix": prefix, "last_key": page_rows[-1]["key"]}
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
        partition_path = [index.partition_key]
        partition_value = json.dumps(key)

        if index.sort_key is None:
            last_key = decoded.get("last_key")
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT key, value
                    FROM skaal_kv
                    WHERE ns = $1
                      AND (value #> $2::text[]) = $3::jsonb
                      AND ($4::text IS NULL OR key > $4)
                    ORDER BY key
                    LIMIT $5
                    """,
                    self.namespace,
                    partition_path,
                    partition_value,
                    last_key,
                    limit + 1,
                )
            page_rows = rows[:limit]
            has_more = len(rows) > limit
            items = [_decode_jsonb(row["value"]) for row in page_rows]
            next_cursor = None
            if has_more and page_rows:
                next_cursor = _encode_cursor(
                    {
                        "mode": "index",
                        "index_name": index_name,
                        "key": _cursor_identity(key),
                        "last_key": page_rows[-1]["key"],
                    }
                )
            return Page(items=items, next_cursor=next_cursor, has_more=has_more)

        sort_path = [index.sort_key]
        if decoded.get("has_last_sort"):
            last_sort = json.dumps(decoded.get("last_sort"))
            last_key = decoded.get("last_key")
            query = """
                SELECT key, value, (value #> $4::text[]) AS sort_value
                FROM skaal_kv
                WHERE ns = $1
                  AND (value #> $2::text[]) = $3::jsonb
                  AND (
                    (value #> $4::text[]) > $5::jsonb
                    OR ((value #> $4::text[]) = $5::jsonb AND key > $6)
                  )
                ORDER BY sort_value, key
                LIMIT $7
            """
            params = [
                self.namespace,
                partition_path,
                partition_value,
                sort_path,
                last_sort,
                last_key,
                limit + 1,
            ]
        else:
            query = """
                SELECT key, value, (value #> $4::text[]) AS sort_value
                FROM skaal_kv
                WHERE ns = $1
                  AND (value #> $2::text[]) = $3::jsonb
                ORDER BY sort_value, key
                LIMIT $5
            """
            params = [self.namespace, partition_path, partition_value, sort_path, limit + 1]

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        page_rows = rows[:limit]
        has_more = len(rows) > limit
        items = [_decode_jsonb(row["value"]) for row in page_rows]
        next_cursor = None
        if has_more and page_rows:
            next_cursor = _encode_cursor(
                {
                    "mode": "index",
                    "index_name": index_name,
                    "key": _cursor_identity(key),
                    "has_last_sort": True,
                    "last_sort": _decode_jsonb(page_rows[-1]["sort_value"]),
                    "last_key": page_rows[-1]["key"],
                }
            )
        return Page(items=items, next_cursor=next_cursor, has_more=has_more)

    async def increment_counter(self, key: str, delta: int = 1) -> int:
        """Atomically increment a counter using a single Postgres upsert."""
        await self._ensure_connected()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO skaal_kv (ns, key, value)
                VALUES ($1, $2, to_jsonb($3::int))
                ON CONFLICT (ns, key)
                DO UPDATE SET value = to_jsonb((skaal_kv.value::int + $3::int))
                RETURNING value
                """,
                self.namespace,
                key,
                delta,
            )
        if row:
            raw = row["value"]
            return int(json.loads(raw)) if isinstance(raw, str) else int(raw)
        return delta

    async def atomic_update(self, key: str, fn: Callable[[Any], Any]) -> Any:
        """Atomically read, apply *fn*, and write the result back.

        Uses a serializable transaction + ``SELECT ... FOR UPDATE`` so no
        concurrent session can observe or overwrite the row between the read
        and the write.  Serialization failures surface as
        :class:`skaal.errors.SkaalConflict`; callers may retry.
        """
        import asyncpg

        await self._ensure_connected()
        try:
            async with self._pool.acquire() as conn:
                async with conn.transaction(isolation="serializable"):
                    row = await conn.fetchrow(
                        "SELECT value FROM skaal_kv WHERE ns = $1 AND key = $2 FOR UPDATE",
                        self.namespace,
                        key,
                    )
                    raw = row["value"] if row is not None else None
                    if isinstance(raw, str):
                        current = json.loads(raw)
                    else:
                        current = raw
                    updated = fn(current)
                    await conn.execute(
                        """
                        INSERT INTO skaal_kv (ns, key, value)
                        VALUES ($1, $2, $3::jsonb)
                        ON CONFLICT (ns, key) DO UPDATE SET value = excluded.value
                        """,
                        self.namespace,
                        key,
                        json.dumps(updated),
                    )
                    return updated
        except asyncpg.exceptions.SerializationError as exc:
            raise SkaalConflict(f"atomic_update on {key!r} lost a race") from exc
        except (
            asyncpg.exceptions.ConnectionDoesNotExistError,
            asyncpg.exceptions.InterfaceError,
        ) as exc:
            raise SkaalUnavailable(f"Postgres unavailable: {exc}") from exc

    async def ensure_relational_schema(self, model_cls: type) -> None:
        """Create missing SQLModel tables for *model_cls*."""
        await self._ensure_relational_engine()
        typed_model = cast(Any, model_cls)
        async with self._engine.begin() as conn:
            await conn.run_sync(typed_model.metadata.create_all)

    @asynccontextmanager
    async def open_relational_session(self, model_cls: type) -> AsyncIterator[Any]:
        """Yield an AsyncSession bound to this backend's PostgreSQL engine."""
        await self.ensure_relational_schema(model_cls)
        assert self._session_factory is not None
        async with self._session_factory() as session:
            yield session

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._pool_loop = None
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None

    def __repr__(self) -> str:
        return f"PostgresBackend(dsn={self.dsn!r}, namespace={self.namespace!r})"
