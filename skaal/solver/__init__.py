"""Z3-based constraint solver for Skaal infrastructure planning."""

from skaal.solver.explain import explain_plan
from skaal.solver.graph import ResourceGraph, build_graph
from skaal.solver.plan import plan, plan_diff
from skaal.solver.solver import solve
from skaal.solver.stability import PlanDiff, StabilityVerdict, diff_plans

__all__ = [
    "PlanDiff",
    "ResourceGraph",
    "StabilityVerdict",
    "build_graph",
    "diff_plans",
    "explain_plan",
    "plan",
    "plan_diff",
    "solve",
]
