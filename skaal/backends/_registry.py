from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from typing import TYPE_CHECKING

from skaal.backends._spec import BackendSpec
from skaal.deploy.kinds import StorageKind

if TYPE_CHECKING:
    from skaal.plan import StorageSpec


@dataclass
class BackendRegistry:
    _plugins: dict[str, BackendSpec] = field(default_factory=dict)

    def register(self, plugin: BackendSpec) -> BackendSpec:
        if plugin.name in self._plugins:
            raise ValueError(f"Backend plugin {plugin.name!r} is already registered.")
        self._plugins[plugin.name] = plugin
        return plugin

    def get(self, name: str) -> BackendSpec:
        try:
            return self._plugins[name]
        except KeyError as exc:
            known = sorted(self._plugins)
            raise ValueError(f"Unknown backend {name!r}. Registered backends: {known}") from exc

    def get_impl(self, name: str) -> type[object]:
        wiring = self.get(name).wiring
        impl = getattr(wiring, "impl", None)
        if impl is not None:
            return impl
        return getattr(import_module(wiring.import_module_name), wiring.import_class_name)

    def resolve(self, spec: "StorageSpec", *, target: str | None = None) -> BackendSpec:
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


backend_registry = BackendRegistry()


def register_backend_spec(plugin: BackendSpec) -> BackendSpec:
    return backend_registry.register(plugin)


def get_backend_spec(name: str) -> BackendSpec:
    return backend_registry.get(name)


def get_backend_impl(name: str) -> type[object]:
    return backend_registry.get_impl(name)


def resolve_backend_spec(spec: "StorageSpec", *, target: str | None = None) -> BackendSpec:
    return backend_registry.resolve(spec, target=target)


register_backend_plugin = register_backend_spec
get_backend_plugin = get_backend_spec
resolve_backend_plugin = resolve_backend_spec


from skaal.backends import BUILTIN_BACKENDS  # noqa: E402

for _plugin in BUILTIN_BACKENDS:
    register_backend_spec(_plugin)


__all__ = [
    "BackendRegistry",
    "backend_registry",
    "get_backend_impl",
    "get_backend_plugin",
    "get_backend_spec",
    "register_backend_plugin",
    "register_backend_spec",
    "resolve_backend_plugin",
    "resolve_backend_spec",
]
