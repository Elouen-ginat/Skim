from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, List, cast

from skaal.backends._spec import BackendSpec, Wiring
from skaal.deploy.kinds import StorageKind


class SqliteBackend:
    def __init__(
        self,
        path: str | Path = "skaal_local.db",
        namespace: str = "default",
    ) -> None:
        self.path = Path(path)
        self.namespace = namespace
        self._db: Any = None
        self._engine: Any = None
        self._session_factory: Any = None

    def _sqlalchemy_url(self) -> str:
        raw = str(self.path)
        if raw == ":memory:":
            return "sqlite+aiosqlite:///:memory:"
        return f"sqlite+aiosqlite:///{self.path.resolve().as_posix()}"

    async def connect(self) -> None:
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

    async def scan(self, prefix: str = "") -> List[tuple[str, Any]]:
        await self._ensure_connected()
        escaped_prefix = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        async with self._db.execute(
            "SELECT key, value FROM kv WHERE ns = ? AND key LIKE ? ESCAPE '\\'",
            (self.namespace, f"{escaped_prefix}%"),
        ) as cursor:
            rows = await cursor.fetchall()
        return [(row[0], json.loads(row[1])) for row in rows]

    async def increment_counter(self, key: str, delta: int = 1) -> int:
        await self._ensure_connected()
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
        await self._ensure_relational_engine()
        typed_model = cast(Any, model_cls)
        async with self._engine.begin() as conn:
            await conn.run_sync(typed_model.metadata.create_all)

    @asynccontextmanager
    async def open_relational_session(self, model_cls: type) -> AsyncIterator[Any]:
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


SQLITE_SPEC = BackendSpec(
    name="sqlite",
    kinds=frozenset({StorageKind.KV, StorageKind.RELATIONAL}),
    wiring=Wiring(
        class_name="SqliteBackend",
        module="skaal.backends.kv.sqlite",
        env_prefix="SKAAL_SQLITE_PATH",
        path_default="skaal_local.db",
        local_env_value="/app/data/skaal.db",
        dependency_sets=("sqlite-driver",),
    ),
    supported_targets=frozenset({"local"}),
)

__all__ = ["SQLITE_SPEC", "SqliteBackend"]
