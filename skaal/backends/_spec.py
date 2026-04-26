"""Canonical backend spec and wiring contract."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, replace
from importlib import import_module
from typing import TYPE_CHECKING, Any, Mapping

from skaal.deploy.kinds import StorageKind

if TYPE_CHECKING:
    from skaal.plan import StorageSpec


@dataclass(frozen=True)
class Wiring:
    """How to build a backend instance from the runtime's entry point."""

    class_name: str
    module: str
    impl: type[Any] | None = None
    env_prefix: str | None = None
    path_default: str | None = None
    connection_value: str | None = None
    uses_namespace: bool = False
    constructor_kwargs: dict[str, Any] = field(default_factory=dict)
    dependency_sets: tuple[str, ...] = ()
    requires_vpc: bool = False
    local_service: str | None = None
    local_env_value: str | None = None

    @property
    def import_class_name(self) -> str:
        if self.impl is not None:
            return self.impl.__name__
        return self.class_name

    @property
    def import_module_name(self) -> str:
        if self.impl is not None:
            return self.impl.__module__
        if "." in self.module:
            return self.module
        return f"skaal.backends.{self.module}"

    @property
    def import_statement(self) -> str:
        return f"from {self.import_module_name} import {self.import_class_name}"

    def load_impl(self) -> type[Any]:
        if self.impl is not None:
            return self.impl
        return getattr(import_module(self.import_module_name), self.import_class_name)

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
            if env_var in source:
                return source[env_var]
        return self.path_default

    def instantiate(self, class_name: str, *, env: Mapping[str, str] | None = None) -> Any:
        backend_cls = self.load_impl()
        value = self.value(class_name, env=env)
        kwargs = dict(self.constructor_kwargs)
        if self.uses_namespace:
            kwargs.setdefault("namespace", class_name)
        if value is None:
            return backend_cls(**kwargs)
        return backend_cls(value, **kwargs)

    def constructor(self, class_name: str) -> str:
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
            return f"{self.import_class_name}()"
        return f"{self.import_class_name}({', '.join(parts)})"


@dataclass
class BackendSpec:
    """Everything Skaal knows about one storage backend."""

    name: str
    kinds: frozenset[StorageKind]
    wiring: Wiring
    supported_targets: frozenset[str] = frozenset()
    local_fallbacks: dict[StorageKind, str] = field(default_factory=dict)

    @property
    def impl(self) -> type[Any]:
        return self.wiring.load_impl()

    def supports(self, target: str) -> bool:
        return target in self.supported_targets

    def fallback_for(self, kind: StorageKind) -> str | None:
        return self.local_fallbacks.get(kind)

    def instantiate(self, resource_name: str, *, env: Mapping[str, str] | None = None) -> Any:
        return self.wiring.instantiate(resource_name, env=env)


BackendPlugin = BackendSpec


def resolve_wiring(plugin: BackendSpec, spec: "StorageSpec") -> Wiring:
    """Return the wiring to use for *spec* given its plan entry."""
    if plugin.name != spec.backend:
        return plugin.wiring

    overrides = dict(spec.wire_params)
    if "extra_deps" in overrides and "dependency_sets" not in overrides:
        overrides["dependency_sets"] = tuple(overrides.pop("extra_deps"))
    if "dependency_sets" in overrides:
        overrides["dependency_sets"] = tuple(overrides["dependency_sets"])
    if "impl" not in overrides and ({"class_name", "module"} & set(overrides)):
        overrides["impl"] = None

    valid_fields = {field.name for field in fields(Wiring)}
    unknown = sorted(set(overrides) - valid_fields)
    if unknown:
        raise ValueError(f"Unsupported wire params for backend {plugin.name!r}: {unknown}")
    return replace(plugin.wiring, **overrides)


__all__ = ["BackendPlugin", "BackendSpec", "Wiring", "resolve_wiring"]
