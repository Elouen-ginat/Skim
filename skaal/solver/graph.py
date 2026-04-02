"""Dependency graph builder for Skaal resource declarations.

Builds a directed acyclic graph (DAG) of resource dependencies so that
the solver and deployer can determine correct provisioning order and detect
cycles.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from skaal.app import App


class CyclicDependencyError(Exception):
    """Raised when the resource dependency graph contains a cycle."""


class ResourceGraph:
    """
    Directed graph of resource name → set of dependency names.

    Nodes are qualified resource names (e.g. ``"auth.Sessions"``).
    Edges represent an explicit ``collocate_with`` or ``depends_on``
    relationship.
    """

    def __init__(self) -> None:
        self._nodes: set[str] = set()
        self._edges: dict[str, set[str]] = {}

    # ── Construction ──────────────────────────────────────────────────────────

    def add_node(self, name: str) -> None:
        self._nodes.add(name)
        self._edges.setdefault(name, set())

    def add_edge(self, src: str, dst: str) -> None:
        """Add a directed edge ``src → dst`` (src depends on dst)."""
        self.add_node(src)
        self.add_node(dst)
        self._edges[src].add(dst)

    # ── Queries ───────────────────────────────────────────────────────────────

    @property
    def nodes(self) -> frozenset[str]:
        return frozenset(self._nodes)

    def dependencies(self, name: str) -> frozenset[str]:
        """Return direct dependencies of *name*."""
        return frozenset(self._edges.get(name, set()))

    def topological_order(self) -> list[str]:
        """
        Return nodes in topological order (dependencies before dependents).

        An edge ``src → dst`` (``add_edge(src, dst)``) means "src depends on
        dst", so dst is scheduled *before* src.

        Raises :class:`CyclicDependencyError` if a cycle is detected.
        """
        # in_degree[n] = number of unresolved dependencies n still has.
        # Each add_edge(src, dst) adds one dependency to src.
        in_degree: dict[str, int] = {n: 0 for n in self._nodes}
        # successors[dst] = set of nodes that depend on dst (unblocked when dst done)
        successors: dict[str, set[str]] = {n: set() for n in self._nodes}
        for src, dsts in self._edges.items():
            for dst in dsts:
                in_degree[src] = in_degree.get(src, 0) + 1
                successors.setdefault(dst, set()).add(src)

        queue: deque[str] = deque(
            sorted(n for n, d in in_degree.items() if d == 0)
        )
        order: list[str] = []

        while queue:
            node = queue.popleft()
            order.append(node)
            for dependent in sorted(successors.get(node, set())):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        if len(order) != len(self._nodes):
            cycle_nodes = sorted(self._nodes - set(order))
            raise CyclicDependencyError(
                f"Cycle detected among resources: {cycle_nodes}"
            )

        return order


def build_graph(app: "App") -> ResourceGraph:
    """
    Build a :class:`ResourceGraph` from an app's registered resources.

    Reads ``__skim_compute__.collocate_with`` on functions to infer edges.
    """
    graph = ResourceGraph()
    all_resources: dict[str, Any] = app._collect_all()

    for qname in all_resources:
        graph.add_node(qname)

    for qname, obj in all_resources.items():
        if callable(obj) and hasattr(obj, "__skim_compute__"):
            compute = obj.__skim_compute__
            target = getattr(compute, "collocate_with", None)
            if target:
                graph.add_edge(qname, target)

    return graph
