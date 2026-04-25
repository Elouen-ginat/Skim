"""Unified backend plugin contract.

The deploy runtime only needs three pieces of information from a backend
plugin today:

1. identity and supported storage kinds
2. runtime wiring for entry-point generation
3. target support and local fallback mapping

Adding a backend is a single file registering a :class:`BackendPlugin`
in :mod:`skaal.deploy.backends`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, replace
from importlib import import_module
from typing import TYPE_CHECKING, Any, Mapping

from skaal.deploy.kinds import StorageKind

if TYPE_CHECKING:
    from skaal.plan import StorageSpec


# ── Wiring (runtime instantiation) ───────────────────────────────────────────


@dataclass(frozen=True)
class Wiring:
    """How to build a backend instance from the runtime's entry point."""

    class_name: str
    """Concrete Python class name, e.g. ``"DynamoBackend"``."""

    module: str
    """Module under :mod:`skaal.backends`, e.g. ``"dynamodb_backend"``."""

    env_prefix: str | None = None
    """Prefix for the connection env-var.

    Full variable name: ``{env_prefix}_{CLASS_NAME.upper()}`` — ``None``
    means no connection string (e.g. in-memory, file-based)."""

    path_default: str | None = None
    """Positional argument for file-based backends (SQLite, local Chroma)."""

    connection_value: str | None = None
    """Explicit constructor value used instead of an env-var lookup or path default."""

    uses_namespace: bool = False
    """Whether the constructor takes ``namespace=...`` as a keyword arg."""

    constructor_kwargs: dict[str, Any] = field(default_factory=dict)
    """Additional keyword args passed to the backend constructor."""

    dependency_sets: tuple[str, ...] = ()
    """Named dependency sets added to the artifact ``pyproject.toml``."""

    requires_vpc: bool = False
    """Whether this backend requires VPC / private-network access."""

    local_service: str | None = None
    """Docker Compose sidecar service name required by this backend."""

    local_env_value: str | None = None
    """Static DSN / URL injected for local Docker Compose builds."""

    @property
    def import_statement(self) -> str:
        return f"from skaal.backends.{self.module} import {self.class_name}"

    def env_var(self, class_name: str) -> str | None:
        if self.env_prefix is None:
            return None
        return f"{self.env_prefix}_{class_name.upper()}"

    def value(self, class_name: str, *, env: Mapping[str, str] | None = None) -> str | None:
        if self.connection_value is not None:
            return self.connection_value
        env_var = self.env_var(class_name)
        if env_var is not None:
            import os

            source = env or os.environ
            return source[env_var]
        return self.path_default

    def instantiate(self, class_name: str, *, env: Mapping[str, str] | None = None) -> Any:
        backend_cls = getattr(import_module(f"skaal.backends.{self.module}"), self.class_name)
        value = self.value(class_name, env=env)
        kwargs = dict(self.constructor_kwargs)
        if self.uses_namespace:
            kwargs.setdefault("namespace", class_name)
        if value is None:
            return backend_cls(**kwargs)
        return backend_cls(value, **kwargs)

    def constructor(self, class_name: str) -> str:
        """Render the Python expression that instantiates the backend."""
        args: list[str] = []
        kwargs: list[str] = []
        value = self.connection_value
        env_var = self.env_var(class_name)

        if value is not None:
            args.append(json.dumps(value))
        elif env_var is not None:
            args.append(f'os.environ["{env_var}"]')
        elif self.path_default is not None:
            args.append(json.dumps(self.path_default))

        if self.uses_namespace:
            kwargs.append(f'namespace="{class_name}"')
        kwargs.extend(f"{name}={value!r}" for name, value in self.constructor_kwargs.items())

        parts = args + kwargs
        if not parts:
            return f"{self.class_name}()"
        return f"{self.class_name}({', '.join(parts)})"


# ── The plugin itself ────────────────────────────────────────────────────────


@dataclass
class BackendPlugin:
    """Everything Skaal knows about one storage backend."""

    name: str
    """Catalog key, e.g. ``"dynamodb"``."""

    kinds: frozenset[StorageKind]
    """Storage kinds this backend satisfies."""

    wiring: Wiring
    """Runtime-side wiring data."""

    supported_targets: frozenset[str] = frozenset()
    """Canonical target names this backend can deploy to directly."""

    local_fallbacks: dict[StorageKind, str] = field(default_factory=dict)
    """Per-kind local fallback plugins used when a cloud plan is built locally."""

    def supports(self, target: str) -> bool:
        return target in self.supported_targets

    def fallback_for(self, kind: StorageKind) -> str | None:
        return self.local_fallbacks.get(kind)


# ── StorageSpec-aware resolver ───────────────────────────────────────────────


def resolve_wiring(plugin: BackendPlugin, spec: "StorageSpec") -> Wiring:
    """Return the wiring to use for *spec* given its plan entry.

    Hook point for backends whose wiring depends on spec details (e.g. a
    pgvector backend that selects a different runtime class based on the
    plan's ``kind``).  Default: return the plugin's own wiring.
    """
    if plugin.name != spec.backend:
        return plugin.wiring

    overrides = dict(spec.wire_params)
    if "extra_deps" in overrides and "dependency_sets" not in overrides:
        overrides["dependency_sets"] = tuple(overrides.pop("extra_deps"))
    if "dependency_sets" in overrides:
        overrides["dependency_sets"] = tuple(overrides["dependency_sets"])

    valid_fields = {field.name for field in fields(Wiring)}
    unknown = sorted(set(overrides) - valid_fields)
    if unknown:
        raise ValueError(f"Unsupported wire params for backend {plugin.name!r}: {unknown}")
    return replace(plugin.wiring, **overrides)
