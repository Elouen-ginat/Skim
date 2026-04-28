from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, cast

from skaal.plan import PatternSpec, PatternType, StorageSpec


@dataclass(frozen=True)
class PatternSolveContext:
    """Shared inputs for a single pattern solver.

    ``storage_specs`` remains shared mutable state so projection solvers can
    co-locate target storage with the source log.
    """

    qname: str
    pattern_meta: dict[str, Any]
    all_resources: dict[str, Any]
    storage_specs: dict[str, StorageSpec]
    storage_backends: dict[str, Any]
    registered_functions: set[str]
    target: str


PatternSolver = Callable[[PatternSolveContext], PatternSpec]

_REGISTRY: dict[PatternType, PatternSolver] = {}


def register_pattern_solver(
    pattern_type: PatternType,
) -> Callable[[PatternSolver], PatternSolver]:
    """Register a pattern solver.

    Example:
        @register_pattern_solver("event-log")
        def solve_event_log(ctx: PatternSolveContext) -> PatternSpec:
            ...
    """

    def _decorate(fn: PatternSolver) -> PatternSolver:
        if pattern_type in _REGISTRY:
            raise RuntimeError(f"solver already registered for pattern_type {pattern_type!r}")
        _REGISTRY[pattern_type] = fn
        return fn

    return _decorate


def solve_pattern(ctx: PatternSolveContext) -> PatternSpec | None:
    """Resolve and run the solver for ``ctx.pattern_meta['pattern_type']``."""

    pattern_type = ctx.pattern_meta.get("pattern_type")
    if not isinstance(pattern_type, str):
        return None
    solver = _REGISTRY.get(cast(PatternType, pattern_type))
    if solver is None:
        return None
    return solver(ctx)


def collect_function_names(app: Any) -> set[str]:
    """Collect bare and qualified names for registered compute functions."""

    names: set[str] = set()
    for qname, obj in app._collect_all().items():
        if callable(obj) and hasattr(obj, "__skaal_compute__"):
            names.add(qname)
            names.add(qname.rsplit(".", 1)[-1])
    return names


def resolve_resource_qname(obj: Any, all_resources: dict[str, Any]) -> str | None:
    """Reverse-resolve a registered object back to its qualified name."""

    for qname, registered in all_resources.items():
        if registered is obj:
            return qname
    name = getattr(obj, "__name__", None) or type(obj).__name__
    for qname in all_resources:
        if qname == name or qname.endswith(f".{name}"):
            return qname
    return None


def storage_constraints_from_pattern(pattern_meta: dict[str, Any]) -> dict[str, Any]:
    """Adapt EventLog metadata into a ``select_backend`` constraint payload."""

    src = pattern_meta.get("storage", {})
    return {
        "kind": "kv",
        "access_pattern": src.get("access_pattern"),
        "durability": src.get("durability"),
        "write_throughput": src.get("throughput"),
        "retention": None,
        "read_latency": None,
        "write_latency": None,
        "consistency": None,
        "residency": None,
        "size_hint": None,
    }
