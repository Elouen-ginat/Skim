"""External component provisioning helpers, shared across deploy targets.

An *external* component is infrastructure that Skaal does **not** provision:
the user has a Kafka cluster, a Postgres box, a Datadog endpoint — we just need
the runtime app to reach it.  The provisioner's job is narrow:

1. Collect the ``connection_env`` of every external component in the plan.
2. Expose each as an environment variable on the generated app container /
   Lambda / Cloud Run service so the runtime code can read it.
3. Surface a list to deploy-time so the operator knows which secrets must be
   populated before the app boots.

For the **local** target we additionally wire the env var through
``docker-compose`` by forwarding it from the host environment.

Provisioned Skaal components (``Proxy``, ``APIGateway``, ``ScheduleTrigger``)
are handled by target-specific generators and are out of scope here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from skaal.plan import ComponentSpec, PlanFile


_EXTERNAL_KINDS = frozenset(
    {"external-storage", "external-queue", "external-observability", "app-ref"}
)


def iter_external_components(plan: "PlanFile") -> list["ComponentSpec"]:
    """Return every external component declared in *plan*."""
    return [c for c in plan.components.values() if not c.provisioned or c.kind in _EXTERNAL_KINDS]


def external_env_vars(plan: "PlanFile", *, prefix: str = "SKAAL_EXT_") -> dict[str, str]:
    """Build a ``{env_var: source}`` map for every external component.

    - If the component declares ``connection_env``, the *source* is the
      literal env-var name the runtime should read.
    - If it only has an inline ``connection_string`` (via ``config``), the
      source is the inline value — used only for local / dev.

    The returned dict can be merged directly into ``env_vars`` in any deploy
    generator.
    """
    out: dict[str, str] = {}
    for comp in iter_external_components(plan):
        env_name = comp.connection_env
        if env_name:
            out[env_name] = f"${{env:{env_name}}}"
            continue
        # Fallback: flatten the component name into an env var so the runtime
        # has something stable to look up.
        synthetic = f"{prefix}{comp.component_name.upper().replace('-', '_')}"
        inline = comp.config.get("connection_string")
        if inline:
            out[synthetic] = inline
    return out


class ExternalProvisioner(Protocol):
    """Target-specific hook for injecting external-component connectivity."""

    def env_vars(self, plan: "PlanFile") -> dict[str, str]: ...
    def compose_fragment(self, plan: "PlanFile") -> str: ...


class DefaultExternalProvisioner:
    """Generic provisioner used by every target unless overridden."""

    def env_vars(self, plan: "PlanFile") -> dict[str, str]:
        return external_env_vars(plan)

    def compose_fragment(self, plan: "PlanFile") -> str:
        """Render a docker-compose ``environment:`` block fragment.

        Forwards each declared ``connection_env`` from the host to the app
        container so local dev mirrors what deploy artifacts will inject.
        """
        envs = external_env_vars(plan)
        if not envs:
            return ""
        lines = [f"      {name}: ${{{name}}}" for name in sorted(envs)]
        return "\n".join(lines) + "\n"
