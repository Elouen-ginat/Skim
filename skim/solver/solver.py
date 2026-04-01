"""Main solve() entry point — orchestrates storage and compute solvers."""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from skim.plan import ComputeSpec, PlanFile, StorageSpec
from skim.solver.storage import UnsatisfiableConstraints, select_backend

if TYPE_CHECKING:
    from skim.app import App


def load_catalog(path: Path | None = None) -> dict[str, Any]:
    """
    Load catalog TOML. Searches CWD/catalog/aws.toml if path not given.

    Returns the parsed TOML as a dict.
    """
    if path is None:
        path = Path.cwd() / "catalog" / "aws.toml"
    if not path.exists():
        raise FileNotFoundError(
            f"Catalog not found at {path}. "
            "Pass --catalog <path> or ensure catalog/aws.toml exists."
        )
    with open(path, "rb") as f:
        return tomllib.load(f)


def solve(app: "App", catalog: dict[str, Any], target: str = "generic") -> "PlanFile":
    """
    Run the Z3 constraint solver over all registered storage and compute
    declarations, producing a concrete infrastructure plan.

    Args:
        app:     The Skim App whose decorators define the constraints.
        catalog: Parsed TOML catalog entries (backends and their characteristics).
        target:  Deploy target: "generic" | "aws-lambda" | "k8s" | "ecs"

    Returns:
        A PlanFile with concrete backend and instance selections.

    Raises:
        UnsatisfiableConstraints: If no backend can satisfy the declared constraints.
    """
    all_resources = app._collect_all()
    storage_backends = catalog.get("storage", {})
    compute_backends = catalog.get("compute", {})

    storage_specs: dict[str, StorageSpec] = {}
    compute_specs: dict[str, ComputeSpec] = {}

    # ── Solve storage ──────────────────────────────────────────────────────
    for qname, obj in all_resources.items():
        if not (isinstance(obj, type) and hasattr(obj, "__skim_storage__")):
            continue

        constraints = obj.__skim_storage__

        backend_name, reason = select_backend(
            qname,
            constraints,
            storage_backends,
            target=target,
        )

        # Compute a stable schema hash from the class name
        schema_hash = hashlib.sha256(qname.encode()).hexdigest()[:12]

        storage_specs[qname] = StorageSpec(
            variable_name=qname,
            backend=backend_name,
            previous_backend=None,
            migration_plan=None,
            migration_stage=0,
            schema_hash=schema_hash,
            reason=reason,
        )

    # ── Solve compute ──────────────────────────────────────────────────────
    for qname, obj in all_resources.items():
        if not (callable(obj) and hasattr(obj, "__skim_compute__")):
            continue

        compute_constraint = obj.__skim_compute__
        instance_type, reason = _select_compute(qname, compute_constraint, compute_backends, target)

        compute_specs[qname] = ComputeSpec(
            function_name=qname,
            instance_type=instance_type,
            instances="auto",
            previous_instance_type=None,
            reason=reason,
        )

    return PlanFile(
        app_name=app.name,
        version=1,
        previous_version=None,
        deploy_target=target,
        storage=storage_specs,
        compute=compute_specs,
    )


def _select_compute(
    fn_name: str,
    compute: Any,
    compute_backends: dict[str, Any],
    target: str,
) -> tuple[str, str]:
    """
    Select a compute instance type for a function.

    Simple heuristic: pick cheapest instance that satisfies compute_type
    and latency requirements.
    """
    if target == "aws-lambda":
        return "lambda", "serverless Lambda; no persistent compute needed"

    compute_type = "cpu"
    if hasattr(compute, "compute_type"):
        ct = compute.compute_type
        compute_type = ct.value if hasattr(ct, "value") else str(ct)

    # Filter by compute_type
    candidates = []
    for name, spec in compute_backends.items():
        spec_types = spec.get("compute_types", ["cpu"])
        if compute_type in spec_types or compute_type == "any":
            candidates.append((name, spec))

    if not candidates:
        candidates = list(compute_backends.items())

    # Sort by cost (cheapest first)
    candidates.sort(key=lambda x: x[1].get("cost_per_hour", 9999))

    if not candidates:
        return "c5-large", "default compute"

    selected_name, selected_spec = candidates[0]
    cost = selected_spec.get("cost_per_hour", 0)
    display = selected_spec.get("display_name", selected_name)
    reason = f"cheapest {compute_type} instance: {display} @ ${cost}/hr"
    return selected_name, reason
