"""Human-readable explanation of solver decisions.

Given a solved :class:`~skaal.plan.PlanFile`, produces a plain-text or
rich-formatted summary of *why* each backend or instance type was chosen.
For UNSAT outcomes, :func:`render_diagnosis` produces the user-facing block.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from skaal.types.solver import Diagnosis

if TYPE_CHECKING:
    from skaal.plan import PlanFile


def _h(text: str, *, rich: bool) -> str:
    return f"[bold]{text}[/bold]" if rich else text


def _dim(text: str, *, rich: bool) -> str:
    return f"[dim]{text}[/dim]" if rich else text


def _ok(rich: bool) -> str:
    return "[green]✓[/green]" if rich else "OK  "


def _fail(rich: bool) -> str:
    return "[red]✗[/red]" if rich else "FAIL"


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

    lines.append(_h(f"Plan: {plan.app_name}  (v{plan.version} → {plan.deploy_target})", rich=rich))
    lines.append("")

    # Storage decisions
    if plan.storage:
        lines.append(_h("Storage", rich=rich))
        for qname, spec in sorted(plan.storage.items()):
            migration = ""
            if spec.previous_backend and spec.previous_backend != spec.backend:
                migration = f"  [migrating from {spec.previous_backend}]"
            lines.append(f"  {qname}  →  {spec.backend}{migration}")
            if spec.reason:
                lines.append(_dim(f"    {spec.reason}", rich=rich))
        lines.append("")

    # Compute decisions
    if plan.compute:
        lines.append(_h("Compute", rich=rich))
        for qname, cspec in sorted(plan.compute.items()):
            lines.append(f"  {qname}  →  {cspec.instance_type}  (×{cspec.instances})")
            if cspec.reason:
                lines.append(_dim(f"    {cspec.reason}", rich=rich))
        lines.append("")

    # Components
    if plan.components:
        lines.append(_h("Components", rich=rich))
        for qname, compspec in sorted(plan.components.items()):
            impl = compspec.implementation or "(auto)"
            lines.append(f"  {qname}  [{compspec.kind}]  →  {impl}")
            if compspec.reason:
                lines.append(_dim(f"    {compspec.reason}", rich=rich))
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


def render_diagnosis(d: Diagnosis, *, rich: bool = False) -> str:
    """Render a UNSAT :class:`Diagnosis` into a multi-line user-facing block.

    The block names the resource, lists every requested constraint, then for
    each candidate backend shows ✓/✗ per constraint with the offered value
    and the slack on numeric constraints.  Closes with the closest-match
    summary and (when applicable) a single-relax suggestion.
    """
    label = "storage" if d.resource_kind == "storage" else "function"
    lines: list[str] = [f"Cannot plan {label} {d.resource_name!r}.", ""]

    if d.requested:
        lines.append(_h("Requested:", rich=rich))
        for text in d.requested.values():
            lines.append(f"  {text}")
        lines.append("")

    if not d.candidates:
        lines.append(_dim("(no candidates declared in the catalog)", rich=rich))
        return "\n".join(lines).rstrip()

    lines.append(_h(f"Considered {len(d.candidates)} backends; none satisfied:", rich=rich))
    for c in d.candidates:
        lines.append(f"  {_h(c.display_name, rich=rich)}  [{c.backend_name}]")
        for v in c.violations:
            offered = v.offered or "(not declared)"
            slack_text = ""
            if v.slack is not None and v.slack not in (float("inf"),):
                sign = "+" if v.slack >= 0 else ""
                slack_text = f"  [off by {sign}{v.slack:g}]"
            lines.append(
                f"    {_fail(rich)} {v.constraint:<18} requested {v.requested}, "
                f"offered {offered}{slack_text}"
            )
        if not c.violations:
            lines.append(f"    {_ok(rich)} all constraints satisfied")
    lines.append("")

    if d.closest is not None:
        n = len(d.closest.violations)
        plural = "s" if n != 1 else ""
        lines.append(
            _h(
                f"Closest match: {d.closest.display_name} "
                f"[{d.closest.backend_name}] ({n} unmet constraint{plural}).",
                rich=rich,
            )
        )

    if d.suggestion is not None:
        s = d.suggestion
        lines.append(
            f"  → If you can accept {s.constraint} {s.offered} instead of "
            f"{s.requested}, {s.backend_name} would satisfy."
        )

    for note in d.extra_notes:
        lines.append(_dim(note, rich=rich))

    return "\n".join(lines).rstrip()


__all__ = [
    "explain_compute",
    "explain_plan",
    "explain_storage",
    "render_diagnosis",
]
