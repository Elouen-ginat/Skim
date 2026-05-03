"""End-to-end: declaration via @app.function(secrets=) → plan-file → solver."""

from __future__ import annotations

from skaal import App, Secret, Store


def test_function_secrets_decorator_collects_into_module():
    app = App("demo")

    @app.storage()
    class Items(Store[str]):
        pass

    @app.function(secrets=[Secret("DB_DSN"), Secret("API_KEY")])
    async def query() -> str:
        return ""

    secrets = app._collect_secrets()
    assert set(secrets) == {"DB_DSN", "API_KEY"}
    assert hasattr(query, "__skaal_secrets__")
    assert {s.name for s in query.__skaal_secrets__} == {"DB_DSN", "API_KEY"}


def test_module_secret_dedupes_by_value():
    app = App("demo")
    app.secret(Secret("X"))
    app.secret(Secret("X"))  # idempotent
    assert "X" in app._collect_secrets()


def test_module_secret_conflict_raises():
    import pytest

    app = App("demo")
    app.secret(Secret("X"))
    with pytest.raises(ValueError):
        app.secret(Secret("X", provider="aws-secrets-manager", source="arn"))


def test_solver_emits_secrets_into_plan_file(tmp_path, monkeypatch):
    from skaal import api

    app = App("demo")

    @app.function(secrets=[Secret("DB_DSN", provider="env")])
    async def echo() -> None:
        return None

    plan = api.plan(app, target="generic", write=False)
    assert "DB_DSN" in plan.secrets
    assert plan.secrets["DB_DSN"].provider == "env"


def test_external_component_attaches_secret_automatically():
    from skaal import App
    from skaal.components import ExternalStorage

    app = App("demo")
    db = ExternalStorage("legacy", secret=Secret("LEGACY_DB"))
    app.attach(db)

    secrets = app._collect_secrets()
    assert "LEGACY_DB" in secrets
