from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from skaal.deploy.plugin import BackendPlugin, Wiring, resolve_wiring
from skaal.deploy.registry import resolve_backend_plugin

if TYPE_CHECKING:
    from skaal.plan import PlanFile, StorageSpec


@dataclass(frozen=True)
class ResolvedBackend:
    plugin: BackendPlugin
    wiring: Wiring

    @property
    def import_statement(self) -> str:
        return self.wiring.import_statement

    @property
    def requires_vpc(self) -> bool:
        return self.wiring.requires_vpc

    @property
    def local_service(self) -> str | None:
        return self.wiring.local_service

    @property
    def local_env_value(self) -> str | None:
        return self.wiring.local_env_value


def resolve_backend(spec: "StorageSpec", *, target: str | None = None) -> ResolvedBackend:
    plugin = resolve_backend_plugin(spec, target=target)
    return ResolvedBackend(plugin=plugin, wiring=resolve_wiring(plugin, spec))


def build_runtime_wiring(plan: "PlanFile", *, target: str | None = None) -> tuple[str, str]:
    seen: set[str] = set()
    import_lines: list[str] = []
    override_lines: list[str] = []

    for qname, spec in plan.storage.items():
        class_name = qname.split(".")[-1]
        backend = resolve_backend(spec, target=target)
        if backend.import_statement not in seen:
            seen.add(backend.import_statement)
            import_lines.append(backend.import_statement)
        override_lines.append(f'        "{class_name}": {backend.wiring.constructor(class_name)},')

    return "\n".join(import_lines), "\n".join(override_lines)


__all__ = ["ResolvedBackend", "build_runtime_wiring", "resolve_backend"]
