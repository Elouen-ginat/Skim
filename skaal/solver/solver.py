"""Main solve() entry point — orchestrates storage and compute solvers."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from skaal.catalog.loader import load_catalog  # noqa: F401 — re-exported for compat
from skaal.plan import ComponentSpec, ComputeSpec, PlanFile, StorageSpec
from skaal.solver.storage import UnsatisfiableConstraints, select_backend

if TYPE_CHECKING:
    from skaal.app import App


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
    component_specs: dict[str, ComponentSpec] = {}

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
    from skaal.solver.compute import UnsatisfiableComputeConstraints, encode_compute

    for qname, obj in all_resources.items():
        if not (callable(obj) and hasattr(obj, "__skim_compute__")):
            continue

        compute_constraint = obj.__skim_compute__
        try:
            instance_type, reason = encode_compute(
                qname, compute_constraint, compute_backends, target=target
            )
        except UnsatisfiableComputeConstraints:
            # Fall back to cheapest available instance rather than failing
            if compute_backends:
                instance_type = min(compute_backends, key=lambda n: compute_backends[n].get("cost_per_hour", 9999))
                reason = f"fallback: cheapest available ({instance_type})"
            else:
                instance_type = "c5-large"
                reason = "default compute (empty catalog)"

        compute_specs[qname] = ComputeSpec(
            function_name=qname,
            instance_type=instance_type,
            instances="auto",
            previous_instance_type=None,
            reason=reason,
        )

    # ── Solve components ───────────────────────────────────────────────────
    from skaal.components import ComponentBase
    from skaal.solver.components import encode_component

    for comp_name, comp_obj in app._components.items():
        if isinstance(comp_obj, ComponentBase):
            try:
                spec = encode_component(comp_name, comp_obj, catalog, target=target)
                component_specs[comp_name] = spec
            except Exception:  # noqa: BLE001
                pass  # non-critical — components don't block the plan

    return PlanFile(
        app_name=app.name,
        version=1,
        previous_version=None,
        deploy_target=target,
        storage=storage_specs,
        compute=compute_specs,
        components=component_specs,
    )
