"""Component constraint encoding for the Z3 solver."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from skaal.components import ComponentBase
    from skaal.plan import ComponentSpec


def encode_component(
    name: str,
    component: "ComponentBase",
    catalog: dict[str, Any],
) -> "ComponentSpec":
    """
    Resolve a component to a concrete implementation spec.

    For ``ProvisionedComponent`` subclasses (Proxy, APIGateway): selects an
    implementation from the catalog based on throughput/latency constraints
    and the deploy target. Emits a ``ComponentSpec`` with ``provisioned=True``.

    For ``ExternalComponent`` subclasses: emits a pass-through ``ComponentSpec``
    with ``provisioned=False`` and the ``connection_env`` forwarded as-is.

    Args:
        name:      The component's ``.name`` attribute.
        component: The ComponentBase instance.
        catalog:   Parsed TOML catalog (may include a ``[components]`` section
                   in future catalog versions).

    Returns:
        A fully resolved ``ComponentSpec`` for inclusion in ``PlanFile.components``.

    Raises:
        NotImplementedError: Until Phase 3 is complete.
    """
    raise NotImplementedError(
        "encode_component() is not yet implemented. "
        "Run `skaal deploy` once Phase 3 is complete."
    )
