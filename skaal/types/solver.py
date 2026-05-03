"""Types describing solver inputs, outputs, and diagnostics.

The :class:`Diagnosis` family is consumed by
:class:`skaal.errors.UnsatisfiableConstraints` to describe *why* a constraint
set could not be satisfied, in the user's own constraint vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ResourceKind = Literal["storage", "compute"]
"""Which solver pass produced a diagnosis."""


@dataclass(frozen=True)
class Violation:
    """One unsatisfied constraint on one candidate backend.

    Attributes:
        constraint: Constraint key from ``__skaal_storage__`` /
            ``__skaal_compute__`` (e.g. ``"read_latency"``, ``"durability"``).
        requested:  Human-readable rendering of the requested value.
        offered:    Human-readable rendering of what the candidate offers,
            or ``None`` when the candidate does not declare the dimension at all.
        slack:      Numeric distance to satisfaction in the constraint's native
            unit.  Positive means the candidate exceeds the request by that much
            (e.g. ``+4ms`` over ``< 1ms``); negative means it falls short
            (e.g. ``-50GB`` for a ``size_hint`` request).  ``None`` for
            categorical constraints that have no notion of distance.
    """

    constraint: str
    requested: str
    offered: str | None
    slack: float | None = None


@dataclass(frozen=True)
class CandidateReport:
    """How one catalog entry compared against the requested constraints."""

    backend_name: str
    display_name: str
    violations: tuple[Violation, ...]
    cost: float = 0.0

    @property
    def feasible(self) -> bool:
        return not self.violations


@dataclass(frozen=True)
class RelaxSuggestion:
    """A single-constraint relaxation that would make a candidate feasible."""

    backend_name: str
    constraint: str
    requested: str
    offered: str


@dataclass
class Diagnosis:
    """Full UNSAT report for one resource."""

    resource_name: str
    resource_kind: ResourceKind
    requested: dict[str, str]
    candidates: tuple[CandidateReport, ...]
    closest: CandidateReport | None = None
    suggestion: RelaxSuggestion | None = None
    extra_notes: tuple[str, ...] = field(default_factory=tuple)


__all__ = [
    "CandidateReport",
    "Diagnosis",
    "RelaxSuggestion",
    "ResourceKind",
    "Violation",
]
