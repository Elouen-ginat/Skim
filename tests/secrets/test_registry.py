"""Tests for Secret declaration, plan-file shape, and the env-backed registry."""

from __future__ import annotations

import json

import pytest

from skaal import Secret, SecretRegistry
from skaal.errors import SecretMissingError
from skaal.types.secret import ResolvedSecret, SecretRef, SecretSpec

# ── SecretRef declaration ─────────────────────────────────────────────────────


def test_secret_defaults_collapse_to_name():
    ref = Secret("DB_DSN")
    assert ref.env_var == "DB_DSN"
    assert ref.source_id == "DB_DSN"
    assert ref.required is True


def test_secret_explicit_env_overrides_name():
    ref = Secret("DB_DSN", env="POSTGRES_URL")
    assert ref.env_var == "POSTGRES_URL"


def test_secret_invalid_name_rejected():
    with pytest.raises(ValueError):
        Secret("has-dash")


def test_secret_env_provider_rejects_json_field():
    with pytest.raises(ValueError):
        Secret("X", json_field="dsn")


def test_secret_to_spec_resolves_defaults():
    spec = Secret("DB_DSN", provider="aws-secrets-manager", source="arn:aws:...:db").to_spec()
    assert spec.env == "DB_DSN"
    assert spec.source == "arn:aws:...:db"
    assert spec.required is True


def test_secret_spec_round_trip_through_json():
    spec = SecretRef("DB", provider="aws-secrets-manager", source="arn:abc").to_spec()
    payload = spec.model_dump_json()
    restored = SecretSpec.model_validate_json(payload)
    assert restored == spec


# ── EnvResolver via SecretRegistry ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_env_resolver_reads_environment(monkeypatch):
    monkeypatch.setenv("DB_DSN", "postgres://localhost/x")
    registry = SecretRegistry({"DB_DSN": Secret("DB_DSN").to_spec()})
    await registry.warmup()
    assert await registry.get("DB_DSN") == "postgres://localhost/x"


@pytest.mark.asyncio
async def test_env_resolver_uses_explicit_env_name(monkeypatch):
    monkeypatch.setenv("OTHER", "value")
    registry = SecretRegistry({"DB": Secret("DB", env="OTHER").to_spec()})
    await registry.warmup()
    assert await registry.get("DB") == "value"


@pytest.mark.asyncio
async def test_required_missing_raises_at_warmup(monkeypatch):
    monkeypatch.delenv("MISSING", raising=False)
    registry = SecretRegistry({"MISSING": Secret("MISSING").to_spec()})
    with pytest.raises(SecretMissingError) as exc_info:
        await registry.warmup()
    assert exc_info.value.name == "MISSING"
    assert exc_info.value.provider == "env"


@pytest.mark.asyncio
async def test_optional_missing_returns_none(monkeypatch):
    monkeypatch.delenv("OPTIONAL", raising=False)
    registry = SecretRegistry({"OPTIONAL": Secret("OPTIONAL", required=False).to_spec()})
    await registry.warmup()
    assert await registry.get("OPTIONAL") is None


@pytest.mark.asyncio
async def test_unknown_get_raises_keyerror():
    registry = SecretRegistry()
    with pytest.raises(KeyError):
        await registry.get("UNKNOWN")


@pytest.mark.asyncio
async def test_get_is_cached(monkeypatch):
    """The resolver is invoked only once per name."""
    calls = {"n": 0}

    class CountingResolver:
        provider = "env"

        async def resolve(self, spec):
            calls["n"] += 1
            return ResolvedSecret(name=spec.name, value=f"v{calls['n']}", provider="env")

        async def close(self):
            pass

    spec = Secret("DB").to_spec()
    registry = SecretRegistry({"DB": spec}, resolvers={"env": CountingResolver()})
    assert await registry.get("DB") == "v1"
    assert await registry.get("DB") == "v1"
    assert calls["n"] == 1


# ── Masking ────────────────────────────────────────────────────────────────────


def test_resolved_secret_repr_masks_value():
    r = ResolvedSecret(name="DB", value="hunter2", provider="env")
    assert "hunter2" not in repr(r)
    assert "***" in repr(r)


def test_resolved_secret_repr_marks_missing():
    r = ResolvedSecret(name="DB", value=None, provider="env")
    assert "<missing>" in repr(r)


# ── JSON-field plucking ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_json_field_plucks_value(monkeypatch):
    monkeypatch.setenv("RAW", json.dumps({"dsn": "postgres://x", "user": "skaal"}))

    class JsonResolver:
        provider = "aws-secrets-manager"

        async def resolve(self, spec):
            import os

            return ResolvedSecret(
                name=spec.name,
                value=os.environ.get(spec.env),
                provider=self.provider,
            )

        async def close(self):
            pass

    spec = Secret(
        "DB", provider="aws-secrets-manager", source="arn", env="RAW", json_field="dsn"
    ).to_spec()
    registry = SecretRegistry({"DB": spec}, resolvers={"aws-secrets-manager": JsonResolver()})
    assert await registry.get("DB") == "postgres://x"


@pytest.mark.asyncio
async def test_json_field_missing_raises(monkeypatch):
    monkeypatch.setenv("RAW", json.dumps({"user": "skaal"}))

    class JsonResolver:
        provider = "aws-secrets-manager"

        async def resolve(self, spec):
            import os

            return ResolvedSecret(
                name=spec.name,
                value=os.environ.get(spec.env),
                provider=self.provider,
            )

        async def close(self):
            pass

    spec = Secret(
        "DB", provider="aws-secrets-manager", source="arn", env="RAW", json_field="dsn"
    ).to_spec()
    registry = SecretRegistry({"DB": spec}, resolvers={"aws-secrets-manager": JsonResolver()})
    with pytest.raises(SecretMissingError):
        await registry.get("DB")


# ── Re-declaration ────────────────────────────────────────────────────────────


def test_registry_redeclare_same_ok():
    registry = SecretRegistry()
    ref = Secret("DB")
    registry.declare(ref)
    registry.declare(ref)  # same shape — no error
    assert "DB" in registry.specs


def test_registry_redeclare_conflict_raises():
    registry = SecretRegistry()
    registry.declare(Secret("DB"))
    with pytest.raises(ValueError):
        registry.declare(Secret("DB", provider="aws-secrets-manager", source="arn"))
