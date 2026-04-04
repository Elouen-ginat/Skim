"""Component constraint encoding and resolution for the Z3 solver.

Resolves ``ProvisionedComponent`` instances to concrete implementations and
passes ``ExternalComponent`` instances through as-is.  Both are written into
``PlanFile.components`` so the deploy engine can provision or configure them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from skaal.components import ComponentBase
    from skaal.plan import ComponentSpec


# ── Implementation selection heuristics ──────────────────────────────────────

#: Default proxy implementations per deploy target
_PROXY_DEFAULTS: dict[str, str] = {
    "k8s": "traefik",
    "ecs": "alb",
    "aws-lambda": "api-gateway",
    "generic": "traefik",
}

#: Default API gateway implementations per deploy target
_GATEWAY_DEFAULTS: dict[str, str] = {
    "k8s": "kong",
    "ecs": "api-gateway",
    "aws-lambda": "api-gateway",
    "generic": "kong",
}


def _select_proxy_impl(component: Any, target: str) -> str:
    """Select proxy implementation, respecting explicit pinning."""
    pinned = getattr(component, "implementation", None)
    if pinned:
        return pinned
    return _PROXY_DEFAULTS.get(target, "traefik")


def _select_gateway_impl(component: Any, target: str) -> str:
    """Select API gateway implementation, respecting explicit pinning."""
    pinned = getattr(component, "implementation", None)
    if pinned:
        return pinned
    return _GATEWAY_DEFAULTS.get(target, "kong")


# ── Main entry point ──────────────────────────────────────────────────────────


def encode_component(
    name: str,
    component: "ComponentBase",
    catalog: dict[str, Any],
    target: str = "generic",
) -> "ComponentSpec":
    """
    Resolve a component to a concrete :class:`~skaal.plan.ComponentSpec`.

    - **ProvisionedComponent** (Proxy, APIGateway): selects an implementation
      from the catalog ``[components.*]`` section (if present) or from
      built-in defaults keyed by *target*.  Returns ``provisioned=True``.

    - **ExternalComponent**: returns a pass-through spec with
      ``provisioned=False`` and the ``connection_env`` forwarded as-is.

    Args:
        name:      The component's ``.name`` attribute.
        component: The ComponentBase instance.
        catalog:   Parsed TOML catalog dict (may include ``[components]``).
        target:    Deploy target: ``"generic"`` | ``"aws-lambda"`` | ``"k8s"``
                   | ``"ecs"``.

    Returns:
        A resolved :class:`~skaal.plan.ComponentSpec`.
    """
    from skaal.components import ExternalComponent
    from skaal.plan import ComponentSpec

    kind = component._skim_component_kind
    comp_meta = component.__skim_component__

    if isinstance(component, ExternalComponent):
        return ComponentSpec(
            component_name=name,
            kind=kind,
            implementation=None,
            provisioned=False,
            connection_env=comp_meta.get("connection_env"),
            config={k: v for k, v in comp_meta.items() if k not in ("kind", "name")},
            reason="external component — not provisioned by Skaal",
        )

    # ProvisionedComponent — select implementation
    if kind == "proxy":
        impl = _select_proxy_impl(component, target)
        reason = f"proxy implementation={impl} for target={target}"
    elif kind == "api-gateway":
        impl = _select_gateway_impl(component, target)
        reason = f"api-gateway implementation={impl} for target={target}"
    else:
        # Generic provisioned component — check catalog first
        comp_catalog = catalog.get("components", {})
        if name in comp_catalog:
            impl = comp_catalog[name].get("implementation", kind)
            reason = f"implementation from catalog: {impl}"
        else:
            impl = kind
            reason = f"default implementation for {kind}"

    return ComponentSpec(
        component_name=name,
        kind=kind,
        implementation=impl,
        provisioned=True,
        connection_env=None,
        config={k: v for k, v in comp_meta.items() if k not in ("kind", "name")},
        reason=reason,
    )
