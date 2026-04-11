"""Component constraint encoding and resolution for the Z3 solver.

Resolves :class:`~skaal.components.ProvisionedComponent` instances to concrete
implementations and passes :class:`~skaal.components.ExternalComponent`
instances through as-is.  Both are written into ``PlanFile.components`` so the
deploy engine can provision or configure them.

Adding support for a new component kind
----------------------------------------
1. Add a ``"<kind>": { ... }`` entry to :data:`_COMPONENT_DEFAULTS` mapping
   each :class:`~skaal.solver.targets.TargetFamily` value to a default
   implementation name.
2. Add a fallback string to :data:`_COMPONENT_FALLBACKS`.
No other changes are required.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from skaal.solver.targets import TargetFamily, resolve_family

if TYPE_CHECKING:
    from skaal.components import ComponentBase
    from skaal.plan import ComponentSpec


# ── Implementation selection tables ──────────────────────────────────────────
#
# Keyed by component kind → target family value → default implementation.
# Family values (TargetFamily.value) are used as keys so the table is readable
# without importing the enum at every call site.

_COMPONENT_DEFAULTS: dict[str, dict[str, str]] = {
    "proxy": {
        TargetFamily.AWS.value: "api-gateway",
        TargetFamily.GCP.value: "cloud-endpoints",
        TargetFamily.LOCAL.value: "traefik",
        TargetFamily.GENERIC.value: "traefik",
        # Container-orchestration overrides within GENERIC family
        "k8s": "traefik",
        "ecs": "alb",
    },
    "api-gateway": {
        TargetFamily.AWS.value: "api-gateway",
        TargetFamily.GCP.value: "cloud-endpoints",
        TargetFamily.LOCAL.value: "kong",
        TargetFamily.GENERIC.value: "kong",
        "k8s": "kong",
        "ecs": "api-gateway",
    },
    "schedule-trigger": {
        TargetFamily.AWS.value: "eventbridge",
        TargetFamily.GCP.value: "cloud-scheduler",
        TargetFamily.LOCAL.value: "apscheduler",
        TargetFamily.GENERIC.value: "apscheduler",
        "k8s": "apscheduler",
        "ecs": "eventbridge",
    },
}

#: Fallback implementation when neither the target nor its family has an entry.
_COMPONENT_FALLBACKS: dict[str, str] = {
    "proxy": "traefik",
    "api-gateway": "kong",
    "schedule-trigger": "apscheduler",
}


# ── Resolution logic ──────────────────────────────────────────────────────────


def _resolve_provisioned_impl(
    name: str,
    kind: str,
    component: Any,
    target: str,
    catalog: dict[str, Any],
) -> tuple[str, str]:
    """Return ``(implementation, reason)`` for a :class:`ProvisionedComponent`.

    Resolution order:
    1. Explicit ``implementation`` pin on the component instance.
    2. Exact target match in :data:`_COMPONENT_DEFAULTS` (e.g. ``"ecs"``).
    3. Target-family match in :data:`_COMPONENT_DEFAULTS` (e.g. ``"aws"``).
    4. Catalog ``[components.<name>]`` entry.
    5. The *kind* string itself as a last resort.
    """
    # 1. Explicit pin takes absolute precedence
    pinned = getattr(component, "implementation", None)
    if pinned:
        return pinned, f"{kind} implementation={pinned!r} (explicitly pinned)"

    defaults = _COMPONENT_DEFAULTS.get(kind)
    if defaults is not None:
        # 2. Exact target string (handles special cases like "ecs" inside GENERIC)
        impl = defaults.get(target)
        if impl is not None:
            return impl, f"{kind} implementation={impl!r} for target={target!r}"

        # 3. Target family
        family_key = resolve_family(target).value
        impl = defaults.get(family_key, _COMPONENT_FALLBACKS.get(kind, kind))
        return impl, f"{kind} implementation={impl!r} for target={target!r}"

    # 4. Catalog lookup for unknown / custom kinds
    comp_catalog = catalog.get("components", {})
    if name in comp_catalog:
        impl = comp_catalog[name].get("implementation") or kind
        return impl, f"implementation {impl!r} from catalog for {kind}"

    # 5. Bare kind name — the deploy engine is expected to recognise it
    return kind, f"default implementation for kind={kind!r}"


# ── Main entry point ──────────────────────────────────────────────────────────


def encode_component(
    name: str,
    component: "ComponentBase",
    catalog: dict[str, Any],
    target: str = "generic",
) -> "ComponentSpec":
    """Resolve a component to a concrete :class:`~skaal.plan.ComponentSpec`.

    - **ProvisionedComponent** (Proxy, APIGateway, ScheduleTrigger): selects
      an implementation via :func:`_resolve_provisioned_impl` and returns a
      spec with ``provisioned=True``.
    - **ExternalComponent**: returns a pass-through spec with
      ``provisioned=False`` and the ``connection_env`` forwarded as-is.

    Args:
        name:      The component's ``.name`` attribute.
        component: The :class:`~skaal.components.ComponentBase` instance.
        catalog:   Parsed TOML catalog dict (may include ``[components]``).
        target:    Deploy target, e.g. ``"generic"`` | ``"aws"`` | ``"k8s"``.

    Returns:
        A resolved :class:`~skaal.plan.ComponentSpec`.
    """
    from skaal.components import ExternalComponent
    from skaal.plan import ComponentSpec

    kind = component._skaal_component_kind
    comp_meta = component.__skaal_component__
    extra_config = {k: v for k, v in comp_meta.items() if k not in ("kind", "name")}

    if isinstance(component, ExternalComponent):
        return ComponentSpec(
            component_name=name,
            kind=kind,
            implementation=None,
            provisioned=False,
            connection_env=comp_meta.get("connection_env"),
            config=extra_config,
            reason="external component — not provisioned by Skaal",
        )

    impl, reason = _resolve_provisioned_impl(name, kind, component, target, catalog)
    return ComponentSpec(
        component_name=name,
        kind=kind,
        implementation=impl,
        provisioned=True,
        connection_env=None,
        config=extra_config,
        reason=reason,
    )
