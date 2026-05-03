"""Secret declaration and resolution types.

A :class:`SecretRef` is what users write at decoration time::

    @app.function(secrets=[Secret("DB_DSN", provider="aws-secrets-manager")])
    async def query(...): ...

The plan-file mirror is :class:`SecretSpec` — same fields with all defaults
resolved so the JSON form is deterministic.  At runtime the
:class:`SecretRegistry` (see :mod:`skaal.secrets`) iterates the spec and
dispatches to a provider-specific :class:`SecretResolver`, materialising a
:class:`ResolvedSecret`.

Provenance is intentionally on the resolved record so structured logs can
say "DB_DSN came from aws-secrets-manager" without printing the value —
``ResolvedSecret.__repr__`` masks it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias, runtime_checkable

from pydantic import BaseModel, ConfigDict

SecretProvider: TypeAlias = Literal[
    "env",
    "aws-secrets-manager",
    "gcp-secret-manager",
    "pulumi-config",
]
"""Where a secret's value comes from at runtime / how deploy wires it."""


_VALID_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class SecretRef:
    """User-facing secret declaration.

    ``name`` is the logical key used by ``app.secrets.get(name)`` and the
    default for both the runtime env var and the provider source identifier.
    """

    name: str
    provider: SecretProvider = "env"
    source: str | None = None
    env: str | None = None
    required: bool = True
    json_field: str | None = None

    def __post_init__(self) -> None:
        if not _VALID_NAME.match(self.name):
            raise ValueError(f"Secret name must match {_VALID_NAME.pattern!r}; got {self.name!r}.")
        if self.env is not None and not _VALID_NAME.match(self.env):
            raise ValueError(f"Secret env must match {_VALID_NAME.pattern!r}; got {self.env!r}.")
        if self.provider == "env" and self.json_field is not None:
            raise ValueError("json_field is not supported for provider='env'.")

    @property
    def env_var(self) -> str:
        return self.env or self.name

    @property
    def source_id(self) -> str:
        return self.source or self.name

    def to_spec(self) -> SecretSpec:
        """Resolve defaults and freeze into the plan-file shape."""
        return SecretSpec(
            name=self.name,
            provider=self.provider,
            source=self.source_id,
            env=self.env_var,
            required=self.required,
            json_field=self.json_field,
        )


class SecretSpec(BaseModel):
    """Plan-file representation. All defaults are resolved.

    Pydantic BaseModel so it serialises through :class:`~skaal.plan.PlanFile`
    without custom hooks.  ``SecretRef.to_spec()`` produces these.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    provider: SecretProvider
    source: str
    env: str
    required: bool = True
    json_field: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedSecret:
    """Runtime carrier — value plus provenance for masked logging."""

    name: str
    value: str | None
    provider: SecretProvider

    def __repr__(self) -> str:
        masked = "***" if self.value else "<missing>"
        return f"ResolvedSecret(name={self.name!r}, value={masked}, provider={self.provider!r})"


@dataclass(frozen=True, slots=True)
class SecretGrant:
    """Deploy-time IAM grant request emitted by :class:`SecretInjector`.

    Builders consume these to attach role policy statements (AWS) or IAM
    bindings (GCP).  ``resource_id`` is provider-shaped: an ARN for AWS,
    a ``projects/.../secrets/<name>`` path for GCP, or the env var name
    for ``env`` / ``pulumi-config`` (where no IAM is needed and the grant
    is informational).
    """

    provider: SecretProvider
    resource_id: str
    actions: tuple[str, ...] = ("read",)


@runtime_checkable
class SecretResolver(Protocol):
    """Strategy that maps a :class:`SecretSpec` to a :class:`ResolvedSecret`."""

    provider: SecretProvider

    async def resolve(self, spec: SecretSpec) -> ResolvedSecret: ...

    async def close(self) -> None: ...


__all__ = [
    "ResolvedSecret",
    "SecretGrant",
    "SecretProvider",
    "SecretRef",
    "SecretResolver",
    "SecretSpec",
]
