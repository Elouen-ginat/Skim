"""Translate a Skim PlanFile into Pulumi Automation API calls."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from skaal.plan import PlanFile


def deploy_from_plan(plan: "PlanFile", *, preview: bool = False) -> dict[str, Any]:
    """
    Provision or update infrastructure described in *plan* using Pulumi.

    Args:
        plan:    The resolved PlanFile (from `skaal plan`).
        preview: If True, run `pulumi preview` instead of `pulumi up`.

    Returns:
        Dictionary of Pulumi stack outputs.

    Raises:
        NotImplementedError: Until Phase 3 is complete.
    """
    raise NotImplementedError(
        "deploy_from_plan() is not yet implemented. "
        "Run `skaal deploy` once Phase 3 is complete."
    )


def rollback(plan: "PlanFile") -> None:
    """Roll back to the previous version using Pulumi."""
    raise NotImplementedError("rollback() is not yet implemented (Phase 3).")
