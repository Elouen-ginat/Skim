"""Tests for relational storage backend selection."""

from __future__ import annotations

from sqlmodel import Field, SQLModel

from skaal import App
from skaal.catalog.loader import load_catalog
from skaal.solver.solver import solve


class User(SQLModel, table=True):
    __tablename__ = "relational_solver_users"

    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)


def _make_relational_app() -> App:
    app = App("relational-solver")
    app.storage(kind="relational", read_latency="< 60ms", durability="persistent")(User)
    return app


def test_solve_local_relational_uses_sqlite() -> None:
    app = _make_relational_app()
    catalog = load_catalog(target="local")

    plan = solve(app, catalog, target="local")
    spec = plan.storage["relational-solver.User"]

    assert spec.kind == "relational"
    assert spec.backend == "sqlite"


def test_solve_aws_relational_uses_rds_postgres() -> None:
    app = _make_relational_app()
    catalog = load_catalog(target="aws")

    plan = solve(app, catalog, target="aws")
    spec = plan.storage["relational-solver.User"]

    assert spec.kind == "relational"
    assert spec.backend == "rds-postgres"
