"""Tests for SQLModel-backed relational storage."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlmodel import Field, SQLModel, select

from skaal import App, ensure_relational_schema, open_relational_session
from skaal.runtime.local import LocalRuntime


def _make_relational_app() -> tuple[App, type[SQLModel]]:
    app = App("relational-demo")

    @app.storage(kind="relational", read_latency="< 20ms", durability="persistent")
    class User(SQLModel, table=True):
        __tablename__ = "users"

        id: int | None = Field(default=None, primary_key=True)
        email: str = Field(index=True, unique=True)
        name: str

    return app, User


def test_relational_decorator_requires_sqlmodel_table() -> None:
    app = App("invalid-relational")

    class Plain:
        pass

    with pytest.raises(TypeError):
        app.storage(kind="relational")(Plain)


@pytest.mark.asyncio
async def test_relational_sqlite_round_trip_creates_real_tables(tmp_path: Path) -> None:
    app, User = _make_relational_app()
    db_path = tmp_path / "relational.db"
    runtime = LocalRuntime.from_sqlite(app, db_path=db_path)

    assert hasattr(User, "__skaal_relational_backend__")

    await ensure_relational_schema(User)

    async with open_relational_session(User) as session:
        alice = User(name="Alice", email="alice@example.com")
        session.add(alice)
        await session.commit()
        await session.refresh(alice)

    async with open_relational_session(User) as session:
        result = await session.exec(select(User).where(User.email == "alice@example.com"))
        users = result.all()

    assert len(users) == 1
    assert users[0].name == "Alice"
    assert users[0].email == "alice@example.com"

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        indexes = conn.execute("PRAGMA index_list('users')").fetchall()

    assert "users" in tables
    assert "kv" not in tables
    assert any(row[2] == 1 for row in indexes)

    for backend in runtime._backends.values():
        await backend.close()
