"""Translate a Skaal PlanFile into Pulumi Automation API calls.

DEPRECATED: The Python Pulumi API integration (deploy_from_plan) is not implemented.
Use the Skaal CLI instead: `skaal deploy <plan>`

The CLI calls Pulumi directly and is the recommended way to deploy from Skaal plans.
Python API support is planned for Phase 3 of the Skaal project.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from skaal.plan import PlanFile


def deploy_from_plan(plan: "PlanFile", *, preview: bool = False) -> dict[str, Any]:
    """
    Provision or update infrastructure described in *plan* using Pulumi.

    .. deprecated::
        The Python Pulumi API is not implemented. Use the CLI instead::

            skaal deploy <plan>

    Args:
        plan:    The resolved PlanFile (from `skaal plan`).
        preview: If True, run `pulumi preview` instead of `pulumi up`.

    Returns:
        Dictionary of Pulumi stack outputs (when implemented).

    Raises:
        NotImplementedError: This API is not yet complete. Use the CLI instead.
    """
    warnings.warn(
        "deploy_from_plan() is deprecated and not yet implemented. "
        "Use the CLI instead: `skaal deploy <plan>`. "
        "Python API support is planned for Phase 3.",
        DeprecationWarning,
        stacklevel=2,
    )
    raise NotImplementedError(
        "deploy_from_plan() is not yet implemented. Use the CLI instead: `skaal deploy <plan>`"
    )


def rollback(plan: "PlanFile") -> None:
    """Roll back to the previous version using Pulumi.

    .. deprecated::
        Not yet implemented. Use the CLI: `skaal deploy --rollback <plan>`
    """
    warnings.warn(
        "rollback() is not yet implemented. Use the CLI instead: `skaal deploy --rollback <plan>`",
        DeprecationWarning,
        stacklevel=2,
    )
    raise NotImplementedError("rollback() is not yet implemented (Phase 3). Use the CLI instead.")
