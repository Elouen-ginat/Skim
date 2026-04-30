"""External component provisioning helpers shared across deploy targets."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from skaal.plan import ComponentSpec, PlanFile


_EXTERNAL_KINDS = frozenset(
    {"external-storage", "external-queue", "external-observability", "app-ref"}
)


def iter_external_components(plan: "PlanFile") -> list["ComponentSpec"]:
    return [
        component
        for component in plan.components.values()
        if not component.provisioned or component.kind in _EXTERNAL_KINDS
    ]


def external_env_vars(plan: "PlanFile", *, prefix: str = "SKAAL_EXT_") -> dict[str, str]:
    out: dict[str, str] = {}
    for component in iter_external_components(plan):
        env_name = component.connection_env
        if env_name:
            out[env_name] = f"${{env:{env_name}}}"
            continue

        synthetic = f"{prefix}{component.component_name.upper().replace('-', '_')}"
        inline = component.config.get("connection_string")
        if inline:
            out[synthetic] = inline
    return out


class ExternalProvisioner(Protocol):
    def env_vars(self, plan: "PlanFile") -> dict[str, str]: ...


class DefaultExternalProvisioner:
    def env_vars(self, plan: "PlanFile") -> dict[str, str]:
        return external_env_vars(plan)
