"""Plan stability analysis — detect drift between plan versions.

Compares a *current* :class:`~skaal.plan.PlanFile` against a *previous* one
and reports:

- Resources added / removed / changed backend or instance type
- Whether the diff is safe to apply without a migration
- An overall stability verdict: ``stable`` | ``drift`` | ``breaking``
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from skaal.plan import PlanFile


class StabilityVerdict(str, Enum):
    STABLE = "stable"  # no changes
    DRIFT = "drift"  # non-breaking changes (new resources, cost tweaks)
    BREAKING = "breaking"  # backend / instance type changed → migration needed


@dataclass
class ResourceDiff:
    name: str
    kind: Literal["storage", "compute"]
    change: Literal["added", "removed", "backend_changed", "instance_changed"]
    old_value: str | None = None
    new_value: str | None = None
    requires_migration: bool = False


@dataclass
class PlanDiff:
    verdict: StabilityVerdict
    diffs: list[ResourceDiff]

    @property
    def is_stable(self) -> bool:
        return self.verdict == StabilityVerdict.STABLE

    @property
    def breaking_changes(self) -> list[ResourceDiff]:
        return [d for d in self.diffs if d.requires_migration]

    def summary(self) -> str:
        if not self.diffs:
            return "No changes detected — plan is stable."
        lines = [f"Verdict: {self.verdict.value}"]
        for d in self.diffs:
            tag = " [MIGRATION REQUIRED]" if d.requires_migration else ""
            lines.append(
                f"  [{d.kind}] {d.name}: {d.change}"
                + (f" ({d.old_value} → {d.new_value})" if d.old_value or d.new_value else "")
                + tag
            )
        return "\n".join(lines)


def diff_plans(old: "PlanFile", new: "PlanFile") -> PlanDiff:
    """
    Compare *old* and *new* plan files and return a :class:`PlanDiff`.

    Backend changes and instance-type changes are flagged as requiring
    migration (``breaking``); new/removed resources are ``drift``.
    """
    diffs: list[ResourceDiff] = []

    # ── Storage diff ──────────────────────────────────────────────────────────
    old_storage = old.storage
    new_storage = new.storage

    for name in set(old_storage) | set(new_storage):
        if name not in old_storage:
            diffs.append(
                ResourceDiff(
                    name=name, kind="storage", change="added", new_value=new_storage[name].backend
                )
            )
            continue
        if name not in new_storage:
            diffs.append(
                ResourceDiff(
                    name=name, kind="storage", change="removed", old_value=old_storage[name].backend
                )
            )
            continue
        old_be = old_storage[name].backend
        new_be = new_storage[name].backend
        if old_be != new_be:
            diffs.append(
                ResourceDiff(
                    name=name,
                    kind="storage",
                    change="backend_changed",
                    old_value=old_be,
                    new_value=new_be,
                    requires_migration=True,
                )
            )

    # ── Compute diff ──────────────────────────────────────────────────────────
    old_compute = old.compute
    new_compute = new.compute

    for name in set(old_compute) | set(new_compute):
        if name not in old_compute:
            diffs.append(
                ResourceDiff(
                    name=name,
                    kind="compute",
                    change="added",
                    new_value=new_compute[name].instance_type,
                )
            )
            continue
        if name not in new_compute:
            diffs.append(
                ResourceDiff(
                    name=name,
                    kind="compute",
                    change="removed",
                    old_value=old_compute[name].instance_type,
                )
            )
            continue
        old_it = old_compute[name].instance_type
        new_it = new_compute[name].instance_type
        if old_it != new_it:
            diffs.append(
                ResourceDiff(
                    name=name,
                    kind="compute",
                    change="instance_changed",
                    old_value=old_it,
                    new_value=new_it,
                    requires_migration=False,
                )
            )

    # ── Verdict ───────────────────────────────────────────────────────────────
    if not diffs:
        verdict = StabilityVerdict.STABLE
    elif any(d.requires_migration for d in diffs):
        verdict = StabilityVerdict.BREAKING
    else:
        verdict = StabilityVerdict.DRIFT

    return PlanDiff(verdict=verdict, diffs=diffs)
