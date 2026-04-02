"""Human-readable explanation of solver decisions.

Given a solved :class:`~skaal.plan.PlanFile`, produces a plain-text or
rich-formatted summary of *why* each backend or instance type was chosen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from skaal.plan import PlanFile


def explain_plan(plan: "PlanFile", *, rich: bool = False) -> str:
    """
    Return a human-readable explanation of every decision in *plan*.

    Args:
        plan: A solved :class:`~skaal.plan.PlanFile`.
        rich: If ``True``, wrap output in Rich markup for coloured display.

    Returns:
        A multi-line string suitable for printing to the terminal.
    """
    lines: list[str] = []

    def h(text: str) -> str:
        return f"[bold]{text}[/bold]" if rich else text

    def dim(text: str) -> str:
        return f"[dim]{text}[/dim]" if rich else text

    lines.append(h(f"Plan: {plan.app_name}  (v{plan.version} → {plan.deploy_target})"))
    lines.append("")

    # Storage decisions
    if plan.storage:
        lines.append(h("Storage"))
        for qname, spec in sorted(plan.storage.items()):
            migration = ""
            if spec.previous_backend and spec.previous_backend != spec.backend:
                migration = f"  [migrating from {spec.previous_backend}]"
            lines.append(f"  {qname}  →  {spec.backend}{migration}")
            if spec.reason:
                lines.append(dim(f"    {spec.reason}"))
        lines.append("")

    # Compute decisions
    if plan.compute:
        lines.append(h("Compute"))
        for qname, spec in sorted(plan.compute.items()):
            lines.append(f"  {qname}  →  {spec.instance_type}  (×{spec.instances})")
            if spec.reason:
                lines.append(dim(f"    {spec.reason}"))
        lines.append("")

    # Components
    if plan.components:
        lines.append(h("Components"))
        for qname, spec in sorted(plan.components.items()):
            impl = spec.implementation or "(auto)"
            lines.append(f"  {qname}  [{spec.kind}]  →  {impl}")
            if spec.reason:
                lines.append(dim(f"    {spec.reason}"))
        lines.append("")

    return "\n".join(lines).rstrip()


def explain_storage(qname: str, reason: str, *, rich: bool = False) -> str:
    """One-line explanation for a single storage decision."""
    prefix = f"  {qname}  →  "
    return prefix + (f"[dim]{reason}[/dim]" if rich else reason)


def explain_compute(qname: str, reason: str, *, rich: bool = False) -> str:
    """One-line explanation for a single compute decision."""
    prefix = f"  {qname}  →  "
    return prefix + (f"[dim]{reason}[/dim]" if rich else reason)
