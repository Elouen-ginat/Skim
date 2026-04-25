from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from skaal.deploy.kinds import StorageKind
from skaal.deploy.plugin import BackendPlugin
from skaal.deploy.targets.base import Target

if TYPE_CHECKING:
    from skaal.plan import StorageSpec


@dataclass
class BackendRegistry:
    _plugins: dict[str, BackendPlugin] = field(default_factory=dict)

    def register(self, plugin: BackendPlugin) -> BackendPlugin:
        if plugin.name in self._plugins:
            raise ValueError(f"Backend plugin {plugin.name!r} is already registered.")
        self._plugins[plugin.name] = plugin
        return plugin

    def get(self, name: str) -> BackendPlugin:
        try:
            return self._plugins[name]
        except KeyError as exc:
            known = sorted(self._plugins)
            raise ValueError(f"Unknown backend {name!r}. Registered backends: {known}") from exc

    def resolve(self, spec: "StorageSpec", *, target: str | None = None) -> BackendPlugin:
        kind = StorageKind.parse(spec.kind)
        plugin = self.get(spec.backend)

        if target == "local":
            fallback = plugin.fallback_for(kind)
            if fallback is not None:
                plugin = self.get(fallback)

        if kind not in plugin.kinds:
            raise ValueError(
                f"Backend {plugin.name!r} does not implement storage kind {kind.value!r}."
            )

        if target is not None and not plugin.supports(target):
            raise ValueError(f"Backend {plugin.name!r} does not support target {target!r}.")

        return plugin

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._plugins))


@dataclass
class TargetRegistry:
    _targets: dict[str, Target] = field(default_factory=dict)

    def register(self, target: Target) -> Target:
        for name in (target.name, *target.aliases):
            if name in self._targets:
                raise ValueError(f"Target {name!r} is already registered.")
            self._targets[name] = target
        return target

    def get(self, name: str) -> Target:
        try:
            return self._targets[name]
        except KeyError as exc:
            known = sorted({target.name for target in self._targets.values()})
            raise ValueError(f"Unknown deploy target {name!r}. Supported targets: {known}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted({target.name for target in self._targets.values()}))


backend_registry = BackendRegistry()
target_registry = TargetRegistry()


def register_backend(plugin: BackendPlugin) -> BackendPlugin:
    return backend_registry.register(plugin)


def register_target(target: Target) -> Target:
    return target_registry.register(target)


def get_backend_plugin(name: str) -> BackendPlugin:
    return backend_registry.get(name)


def resolve_backend_plugin(spec: "StorageSpec", *, target: str | None = None) -> BackendPlugin:
    return backend_registry.resolve(spec, target=target)


def get_target(name: str) -> Target:
    return target_registry.get(name)


from skaal.deploy.backends import BUILTIN_BACKENDS  # noqa: E402
from skaal.deploy.targets import BUILTIN_TARGETS  # noqa: E402

for _plugin in BUILTIN_BACKENDS:
    register_backend(_plugin)

for _target in BUILTIN_TARGETS:
    register_target(_target)


__all__ = [
    "backend_registry",
    "target_registry",
    "get_backend_plugin",
    "get_target",
    "register_backend",
    "register_target",
    "resolve_backend_plugin",
]
