"""Main solve() entry point — orchestrates storage and compute solvers."""

from __future__ import annotations

import hashlib
import inspect
import json
import warnings
from typing import TYPE_CHECKING, Any

from skaal.plan import ComponentSpec, ComputeSpec, PlanFile, StorageSpec
from skaal.solver.storage import select_backend
from skaal.solver.targets import catalog_compute_key

if TYPE_CHECKING:
    from skaal.app import App


def _compute_schema_hash(obj: Any) -> str:
    """
    Compute a stable hash of a class schema from its annotations.

    This hash changes when fields are added, removed, or their types change,
    enabling proper schema migration detection.

    Args:
        obj: The class object to hash.

    Returns:
        A 12-character hex string (first 12 chars of SHA256).
    """
    # Collect all annotations from the class and its bases
    annotations: dict[str, str] = {}
    for base in reversed(inspect.getmro(obj)):
        if base is object:
            continue
        base_annotations = getattr(base, "__annotations__", {})
        for field_name, field_type in base_annotations.items():
            # Normalize type annotations to string
            if hasattr(field_type, "__module__") and hasattr(field_type, "__qualname__"):
                type_str = f"{field_type.__module__}.{field_type.__qualname__}"
            else:
                type_str = str(field_type)
            annotations[field_name] = type_str

    # Sort for stability and JSON-encode
    if not annotations:
        # No annotations; fall back to qualname
        qname = getattr(obj, "__module__", "") + "." + getattr(obj, "__qualname__", "Unknown")
        payload = qname.encode()
    else:
        payload = json.dumps(annotations, sort_keys=True, default=str).encode()

    return hashlib.sha256(payload).hexdigest()[:12]


def solve(app: "App", catalog: dict[str, Any], target: str = "generic") -> "PlanFile":
    """
    Run the Z3 constraint solver over all registered storage and compute
    declarations, producing a concrete infrastructure plan.

    Args:
        app:     The Skaal App whose decorators define the constraints.
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

        # Compute a stable schema hash from the class's annotated fields
        # This changes when fields are added, removed, or their types change
        schema_hash = _compute_schema_hash(obj)

        # Carry deploy-time provisioning params from the catalog into the plan.
        # The solver never reads these; they are only consumed by deploy generators.
        backend_entry = storage_backends.get(backend_name, {})
        deploy_params = backend_entry.get("deploy", {})
        wire_params = backend_entry.get("wire", {})

        storage_specs[qname] = StorageSpec(
            variable_name=qname,
            backend=backend_name,
            previous_backend=None,
            migration_plan=None,
            migration_stage=0,
            schema_hash=schema_hash,
            reason=reason,
            deploy_params=deploy_params,
            wire_params=wire_params,
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
        except UnsatisfiableComputeConstraints as e:
            # Warn the user that their constraint was violated, then fall back to cheapest
            warnings.warn(
                f"Compute constraint for {qname!r} is unsatisfiable: {e}. "
                "Falling back to cheapest available instance.",
                RuntimeWarning,
                stacklevel=2,
            )
            if compute_backends:
                instance_type = min(
                    compute_backends, key=lambda n: compute_backends[n].get("cost_per_hour", 9999)
                )
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
            except Exception as exc:  # noqa: BLE001
                warnings.warn(
                    f"Component {comp_name!r} encoding failed: {exc}. "
                    "It will be omitted from the plan.",
                    RuntimeWarning,
                    stacklevel=2,
                )

    # ── Target-level deploy config ─────────────────────────────────────────
    # Read the deploy params for the target compute backend (e.g. Lambda,
    # Cloud Run) from the catalog.  The solver doesn't use these; deploy
    # generators do.
    target_compute_key = catalog_compute_key(target)
    deploy_config: dict[str, Any] = {}
    if target_compute_key:
        deploy_config = compute_backends.get(target_compute_key, {}).get("deploy", {})

    return PlanFile(
        app_name=app.name,
        version=1,
        previous_version=None,
        deploy_target=target,
        deploy_config=deploy_config,
        storage=storage_specs,
        compute=compute_specs,
        components=component_specs,
    )
