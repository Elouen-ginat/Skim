"""Advanced solver primitives for Skaal infrastructure planning.

Most application code should use :mod:`skaal.api`; this module exposes the
lower-level planning and stability helpers used by tests and advanced tooling.
"""

from skaal.solver.explain import explain_plan
from skaal.solver.graph import ResourceGraph, build_graph
from skaal.solver.solver import solve
from skaal.solver.stability import StabilityReport, StabilityVerdict, diff_plans
from skaal.solver.targets import TargetFamily

__all__ = [
    "ResourceGraph",
    "StabilityReport",
    "StabilityVerdict",
    "TargetFamily",
    "build_graph",
    "diff_plans",
    "explain_plan",
    "solve",
]
