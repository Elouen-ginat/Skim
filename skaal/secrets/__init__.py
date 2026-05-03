"""Runtime secret resolution.

Public surface:

- :data:`Secret`              — alias for :class:`SecretRef`; ``Secret("DB", ...)``.
- :class:`SecretRegistry`     — per-app cache + warmup + dispatch.
- :class:`EnvResolver`        — reads ``os.environ``.

Cloud resolvers live in submodules and are imported lazily so the base
install does not require ``aioboto3`` or the GCP SDK.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from skaal.errors import SecretMissingError
from skaal.types.secret import (
    ResolvedSecret,
    SecretProvider,
    SecretRef,
    SecretResolver,
    SecretSpec,
)

_LOG = logging.getLogger("skaal.secrets")

Secret = SecretRef


class EnvResolver:
    """Reads :class:`SecretSpec` values from ``os.environ``.

    For ``provider='env'`` the value is taken verbatim; for cloud providers
    that inject the live value into the function env at deploy time
    (Cloud Run, Pulumi config) this resolver is also the runtime path.
    """

    def __init__(self, provider: SecretProvider = "env") -> None:
        self.provider: SecretProvider = provider

    async def resolve(self, spec: SecretSpec) -> ResolvedSecret:
        raw = os.environ.get(spec.env)
        if raw is None and spec.source != spec.env:
            # `provider='env'` allows decoupling logical name from env-var name
            raw = os.environ.get(spec.source)
        return ResolvedSecret(name=spec.name, value=raw, provider=self.provider)

    async def close(self) -> None:
        return None


def _build_default_resolvers() -> dict[SecretProvider, SecretResolver]:
    """Resolver dispatch table used by :class:`SecretRegistry` by default.

    Cloud-backed resolvers are imported lazily so a missing extra fails at
    *use* time, not at import time.  See :class:`_LazyAws` and
    :class:`_LazyGcp`.
    """
    return {
        "env": EnvResolver("env"),
        "pulumi-config": EnvResolver("pulumi-config"),
        "gcp-secret-manager": _LazyGcp(),
        "aws-secrets-manager": _LazyAws(),
    }


class _LazyAws:
    """Defer importing :mod:`skaal.secrets.aws` until a secret needs AWS."""

    provider: SecretProvider = "aws-secrets-manager"

    def __init__(self) -> None:
        self._inner: SecretResolver | None = None

    async def resolve(self, spec: SecretSpec) -> ResolvedSecret:
        if self._inner is None:
            from skaal.secrets.aws import AwsSecretsManagerResolver

            self._inner = AwsSecretsManagerResolver()
        return await self._inner.resolve(spec)

    async def close(self) -> None:
        if self._inner is not None:
            await self._inner.close()


class _LazyGcp:
    provider: SecretProvider = "gcp-secret-manager"

    def __init__(self) -> None:
        self._inner: SecretResolver | None = None
        self._fallback = EnvResolver("gcp-secret-manager")

    async def resolve(self, spec: SecretSpec) -> ResolvedSecret:
        # On Cloud Run the value is already in env (env_from.secret_key_ref).
        # Skip the SDK call when env carries it.
        if os.environ.get(spec.env) is not None:
            return await self._fallback.resolve(spec)
        if self._inner is None:
            from skaal.secrets.gcp import GcpSecretManagerResolver

            self._inner = GcpSecretManagerResolver()
        return await self._inner.resolve(spec)

    async def close(self) -> None:
        if self._inner is not None:
            await self._inner.close()


class SecretRegistry:
    """Per-app cache + dispatch for declared secrets.

    The plan-file's ``secrets`` dict is fed in at construction; ``warmup``
    eagerly resolves every ``required=True`` secret so that missing values
    surface at boot, not on the first request handler that touches one.
    """

    def __init__(
        self,
        specs: dict[str, SecretSpec] | None = None,
        *,
        resolvers: dict[SecretProvider, SecretResolver] | None = None,
    ) -> None:
        self._specs: dict[str, SecretSpec] = dict(specs or {})
        self._resolvers: dict[SecretProvider, SecretResolver] = (
            resolvers if resolvers is not None else _build_default_resolvers()
        )
        self._cache: dict[str, ResolvedSecret] = {}
        self._lock = asyncio.Lock()

    @property
    def specs(self) -> dict[str, SecretSpec]:
        return dict(self._specs)

    def declare(self, ref: SecretRef) -> None:
        spec = ref.to_spec()
        existing = self._specs.get(spec.name)
        if existing is not None and existing != spec:
            raise ValueError(
                f"Secret {spec.name!r} re-declared with different parameters: {existing} vs {spec}"
            )
        self._specs[spec.name] = spec

    async def warmup(self) -> None:
        """Resolve every required secret so missing values fail fast."""
        for name, spec in self._specs.items():
            resolved = await self._resolve_locked(spec)
            if resolved.value is None and spec.required:
                raise SecretMissingError(
                    name,
                    spec.provider,
                    detail=f"env={spec.env!r}, source={spec.source!r}",
                )
            _LOG.info(
                "secret %s resolved (provider=%s, value=%s)",
                name,
                spec.provider,
                "present" if resolved.value else "missing",
            )

    async def get(self, name: str) -> str | None:
        """Return the resolved value, fetching and caching on first access."""
        spec = self._specs.get(name)
        if spec is None:
            raise KeyError(f"Secret {name!r} is not declared.")
        resolved = await self._resolve_locked(spec)
        return resolved.value

    async def get_resolved(self, name: str) -> ResolvedSecret:
        spec = self._specs.get(name)
        if spec is None:
            raise KeyError(f"Secret {name!r} is not declared.")
        return await self._resolve_locked(spec)

    async def close(self) -> None:
        for resolver in self._resolvers.values():
            await resolver.close()
        self._cache.clear()

    async def _resolve_locked(self, spec: SecretSpec) -> ResolvedSecret:
        cached = self._cache.get(spec.name)
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._cache.get(spec.name)
            if cached is not None:
                return cached
            resolver = self._resolvers.get(spec.provider)
            if resolver is None:
                raise KeyError(f"No resolver registered for provider {spec.provider!r}")
            resolved = await resolver.resolve(spec)
            resolved = _apply_json_field(spec, resolved)
            self._cache[spec.name] = resolved
            return resolved


def _apply_json_field(spec: SecretSpec, resolved: ResolvedSecret) -> ResolvedSecret:
    if spec.json_field is None or resolved.value is None:
        return resolved
    try:
        payload = json.loads(resolved.value)
    except json.JSONDecodeError as exc:
        raise SecretMissingError(
            spec.name,
            spec.provider,
            detail=f"value is not valid JSON for json_field={spec.json_field!r}: {exc}",
        ) from exc
    if not isinstance(payload, dict) or spec.json_field not in payload:
        raise SecretMissingError(
            spec.name,
            spec.provider,
            detail=f"json_field={spec.json_field!r} not present in payload",
        )
    return ResolvedSecret(
        name=spec.name,
        value=str(payload[spec.json_field]),
        provider=spec.provider,
    )


__all__ = [
    "EnvResolver",
    "Secret",
    "SecretRegistry",
]
