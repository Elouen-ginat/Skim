"""End-to-end tests for autogenerate / upgrade / downgrade / current / history."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Field, SQLModel

from skaal import App, api
from skaal.runtime.local import LocalRuntime


def _make_app(tmp_path: Path) -> tuple[App, type[SQLModel], type[SQLModel]]:
    app = App(name="rel-mig-app")

    @app.storage(kind="relational", read_latency="< 20ms", durability="persistent")
    class User(SQLModel, table=True):
        __tablename__ = f"users_{tmp_path.name}"

        id: int | None = Field(default=None, primary_key=True)
        email: str = Field(unique=True)

    @app.storage(kind="relational", read_latency="< 20ms", durability="persistent")
    class Tag(SQLModel, table=True):
        __tablename__ = f"tags_{tmp_path.name}"

        id: int | None = Field(default=None, primary_key=True)
        name: str

    LocalRuntime.from_sqlite(app, db_path=tmp_path / "rel.db")
    return app, User, Tag


@pytest.mark.asyncio
async def test_autogenerate_writes_revision_with_create_tables(
    isolated_cwd: Path,
) -> None:
    app, User, Tag = _make_app(isolated_cwd)

    revisions = await api.relational_autogenerate(app, message="initial")

    assert "sqlite" in revisions
    rev = revisions["sqlite"]
    assert rev is not None
    assert rev.message == "initial"
    versions_dir = isolated_cwd / ".skaal/migrations/rel-mig-app/relational/sqlite/versions"
    files = list(versions_dir.glob("*.py"))
    assert len(files) == 1
    text = files[0].read_text()
    assert "create_table" in text
    assert User.__tablename__ in text
    assert Tag.__tablename__ in text


@pytest.mark.asyncio
async def test_upgrade_then_current_at_head(isolated_cwd: Path) -> None:
    app, _u, _t = _make_app(isolated_cwd)

    await api.relational_autogenerate(app, message="initial")
    await api.relational_upgrade(app)

    statuses = await api.relational_current(app)
    status = statuses["sqlite"]
    assert status.is_at_head
    assert status.current_revision is not None
    assert status.current_revision == status.head_revision


@pytest.mark.asyncio
async def test_downgrade_to_base_clears_current_revision(isolated_cwd: Path) -> None:
    app, _u, _t = _make_app(isolated_cwd)

    await api.relational_autogenerate(app, message="initial")
    await api.relational_upgrade(app)
    await api.relational_downgrade(app, target="base")

    statuses = await api.relational_current(app)
    assert statuses["sqlite"].current_revision is None


@pytest.mark.asyncio
async def test_history_lists_revisions(isolated_cwd: Path) -> None:
    app, _u, _t = _make_app(isolated_cwd)

    await api.relational_autogenerate(app, message="initial")
    await api.relational_upgrade(app)
    histories = await api.relational_history(app)
    revs = histories["sqlite"]
    assert len(revs) == 1
    assert revs[0].is_applied
    assert revs[0].is_head
