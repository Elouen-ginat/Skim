from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, List, cast

from skaal.backends._spec import BackendSpec, Wiring
from skaal.deploy.kinds import StorageKind
from skaal.errors import SkaalConflict, SkaalUnavailable

_LOCAL_POSTGRES_DSN = "postgresql://skaal_user:skaal_pass@postgres/skaal_db"


class PostgresBackend:
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
        self._pool: Any = None
        self._pool_loop: asyncio.AbstractEventLoop | None = None
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
                pass

    async def _ensure_connected(self) -> None:
        current_loop = asyncio.get_running_loop()
        if self._pool is not None and self._pool_loop is not current_loop:
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
            value = json.loads(raw) if isinstance(raw, str) else raw
            result.append((row["key"], value))
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
            value = json.loads(raw) if isinstance(raw, str) else raw
            result.append((row["key"], value))
        return result

    async def increment_counter(self, key: str, delta: int = 1) -> int:
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
                    current = json.loads(raw) if isinstance(raw, str) else raw
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


RDS_POSTGRES_SPEC = BackendSpec(
    name="rds-postgres",
    kinds=frozenset({StorageKind.KV, StorageKind.RELATIONAL}),
    wiring=Wiring(
        class_name="PostgresBackend",
        module="skaal.backends.kv.postgres",
        env_prefix="SKAAL_DB_DSN",
        uses_namespace=True,
        dependency_sets=("postgres-asyncpg",),
        requires_vpc=True,
        local_service="postgres",
        local_env_value=_LOCAL_POSTGRES_DSN,
    ),
    supported_targets=frozenset({"aws"}),
    local_fallbacks={
        StorageKind.KV: "local-redis",
        StorageKind.RELATIONAL: "sqlite",
    },
)

CLOUD_SQL_POSTGRES_SPEC = BackendSpec(
    name="cloud-sql-postgres",
    kinds=frozenset({StorageKind.KV, StorageKind.RELATIONAL}),
    wiring=Wiring(
        class_name="PostgresBackend",
        module="skaal.backends.kv.postgres",
        env_prefix="SKAAL_DB_DSN",
        uses_namespace=True,
        dependency_sets=("cloud-sql-connector",),
        requires_vpc=True,
        local_service="postgres",
        local_env_value=_LOCAL_POSTGRES_DSN,
    ),
    supported_targets=frozenset({"gcp"}),
    local_fallbacks={
        StorageKind.KV: "local-redis",
        StorageKind.RELATIONAL: "sqlite",
    },
)

__all__ = ["CLOUD_SQL_POSTGRES_SPEC", "PostgresBackend", "RDS_POSTGRES_SPEC"]
