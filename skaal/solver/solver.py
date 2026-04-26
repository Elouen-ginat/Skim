"""Main solve() entry point — orchestrates storage, compute, component, and pattern solvers."""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import warnings
from typing import TYPE_CHECKING, Any

from skaal.plan import ComponentSpec, ComputeSpec, PatternSpec, PlanFile, StorageSpec
from skaal.solver.graph import CyclicDependencyError, build_graph
from skaal.solver.storage import UnsatisfiableConstraints, select_backend
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


# ── Helpers for serialising resilience dataclasses ────────────────────────────


def _policy_to_dict(policy: Any) -> dict[str, Any] | None:
    """Serialise a resilience policy dataclass to a plain dict (JSON-safe)."""
    if policy is None:
        return None
    if dataclasses.is_dataclass(policy) and not isinstance(policy, type):
        return dataclasses.asdict(policy)
    # Fallback: assume a dict-ish payload
    return dict(policy) if hasattr(policy, "keys") else None


def _storage_constraints_from_pattern(pattern_meta: dict[str, Any]) -> dict[str, Any]:
    """
    Build a ``__skaal_storage__``-shaped dict from an ``EventLog`` pattern's
    ``pattern_meta["storage"]`` sub-dict, so it can be fed to ``select_backend``.
    """
    src = pattern_meta.get("storage", {})
    # The source may lack throughput/size_hint/etc. — fill with the few keys
    # the checker registry understands.
    return {
        "kind": "kv",
        "access_pattern": src.get("access_pattern"),
        "durability": src.get("durability"),
        "write_throughput": src.get("throughput"),
        "retention": None,  # retention here is a duration string, not an enum
        "read_latency": None,
        "write_latency": None,
        "consistency": None,
        "residency": None,
        "size_hint": None,
    }


def _collect_all_components(app: "App") -> dict[str, Any]:
    """
    Recursively collect all components from *app* and every mounted submodule.

    Components are not included in ``_collect_all()`` (which only yields
    storage/agents/functions/channels/patterns/schedules).  This helper fills
    that gap so the solver can plan components declared inside modules that are
    mounted into the app via ``app.use()``.
    """
    result: dict[str, Any] = {}

    def _recurse(module: Any) -> None:
        for name, comp in getattr(module, "_components", {}).items():
            if name not in result:  # top-level wins on name collision
                result[name] = comp
        for sub in getattr(module, "_submodules", {}).values():
            _recurse(sub)

    _recurse(app)
    return result


def _collect_function_names(app: "App") -> set[str]:
    """
    Return the set of all function identifiers a Saga step can legally
    reference.  Includes bare names and qualified names so module authors can
    use either form.
    """
    names: set[str] = set()
    for qname, obj in app._collect_all().items():
        if callable(obj) and hasattr(obj, "__skaal_compute__"):
            names.add(qname)
            # Also register the bare leaf name (saga.function → "reserve_inventory")
            names.add(qname.rsplit(".", 1)[-1])
    return names


def _resolve_resource_qname(obj: Any, all_resources: dict[str, Any]) -> str | None:
    """
    Reverse-lookup: given a resource object, return the qualified name it's
    registered under in ``all_resources``.  Used to resolve Projection/Outbox
    cross-references from Python object to plan entry.
    """
    for qname, registered in all_resources.items():
        if registered is obj:
            return qname
    # Fall back to class name for storage classes
    name = getattr(obj, "__name__", None) or type(obj).__name__
    for qname in all_resources:
        if qname == name or qname.endswith(f".{name}"):
            return qname
    return None


def _resolve_collocate(
    raw_colocate: str | None,
    *,
    owner_qname: str,
    owner_kind: str,
    all_resources: dict[str, Any],
) -> str | None:
    """Resolve a ``collocate_with`` hint to a qualified resource name.

    The decorator accepts a bare class name or a qualified name; this
    normalises to a qualified name registered in *all_resources*.  Emits a
    ``RuntimeWarning`` and returns ``None`` if the hint matches nothing.
    """
    if not raw_colocate:
        return None
    if raw_colocate in all_resources:
        return raw_colocate
    for candidate in all_resources:
        if candidate == raw_colocate or candidate.endswith(f".{raw_colocate}"):
            return candidate
    warnings.warn(
        f"{owner_kind} {owner_qname!r}: collocate_with={raw_colocate!r} "
        "does not match any registered resource. Ignored.",
        RuntimeWarning,
        stacklevel=2,
    )
    return None


def _solve_storage(
    all_resources: dict[str, Any],
    storage_backends: dict[str, Any],
    *,
    target: str,
) -> dict[str, StorageSpec]:
    """Pick a backend for every ``__skaal_storage__``-annotated class."""
    storage_specs: dict[str, StorageSpec] = {}
    for qname, obj in all_resources.items():
        if not (isinstance(obj, type) and hasattr(obj, "__skaal_storage__")):
            continue

        constraints = obj.__skaal_storage__
        backend_name, reason = select_backend(qname, constraints, storage_backends, target=target)

        backend_entry = storage_backends.get(backend_name, {})
        deploy_params = backend_entry.get("deploy", {})
        wire_params = backend_entry.get("wire", {})

        colocate_qname = _resolve_collocate(
            constraints.get("collocate_with"),
            owner_qname=qname,
            owner_kind="Storage",
            all_resources=all_resources,
        )

        storage_specs[qname] = StorageSpec(
            variable_name=qname,
            backend=backend_name,
            kind=constraints.get("kind", "kv"),
            previous_backend=None,
            migration_plan=None,
            migration_stage=0,
            schema_hash=_compute_schema_hash(obj),
            reason=reason,
            collocate_with=colocate_qname,
            auto_optimize=bool(constraints.get("auto_optimize", False)),
            deploy_params=deploy_params,
            wire_params=wire_params,
        )
    return storage_specs


def _solve_compute(
    all_resources: dict[str, Any],
    compute_backends: dict[str, Any],
    *,
    target: str,
) -> dict[str, ComputeSpec]:
    """Pick an instance type for every ``__skaal_compute__``-annotated callable."""
    from skaal.solver.compute import UnsatisfiableComputeConstraints, encode_compute

    compute_specs: dict[str, ComputeSpec] = {}
    for qname, obj in all_resources.items():
        if not (callable(obj) and hasattr(obj, "__skaal_compute__")):
            continue

        compute_constraint = obj.__skaal_compute__
        try:
            instance_type, reason = encode_compute(
                qname, compute_constraint, compute_backends, target=target
            )
        except UnsatisfiableComputeConstraints as exc:
            warnings.warn(
                f"Compute constraint for {qname!r} is unsatisfiable: {exc}. "
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

        colocate_qname = _resolve_collocate(
            getattr(compute_constraint, "collocate_with", None),
            owner_qname=qname,
            owner_kind="Function",
            all_resources=all_resources,
        )

        # Scale strategy — set by @scale decorator
        scale_obj = getattr(obj, "__skaal_scale__", None)
        scale_strategy: str | None = None
        instances: int | str = "auto"
        if scale_obj is not None:
            strategy = getattr(scale_obj, "strategy", None)
            if strategy is not None:
                scale_strategy = strategy.value if hasattr(strategy, "value") else str(strategy)
            instances = getattr(scale_obj, "instances", "auto")

        compute_specs[qname] = ComputeSpec(
            function_name=qname,
            instance_type=instance_type,
            instances=instances,
            previous_instance_type=None,
            reason=reason,
            collocate_with=colocate_qname,
            scale_strategy=scale_strategy,
            retry=_policy_to_dict(getattr(compute_constraint, "retry", None)),
            circuit_breaker=_policy_to_dict(getattr(compute_constraint, "circuit_breaker", None)),
            rate_limit=_policy_to_dict(getattr(compute_constraint, "rate_limit", None)),
            bulkhead=_policy_to_dict(getattr(compute_constraint, "bulkhead", None)),
        )
    return compute_specs


def _solve_components(
    app: "App",
    catalog: dict[str, Any],
    *,
    target: str,
) -> dict[str, ComponentSpec]:
    """Encode every attached :class:`ComponentBase` into a spec."""
    from skaal.components import ComponentBase
    from skaal.solver.components import encode_component

    component_specs: dict[str, ComponentSpec] = {}
    for comp_name, comp_obj in _collect_all_components(app).items():
        if not isinstance(comp_obj, ComponentBase):
            continue
        try:
            component_specs[comp_name] = encode_component(
                comp_name, comp_obj, catalog, target=target
            )
        except Exception as exc:  # noqa: BLE001
            warnings.warn(
                f"Component {comp_name!r} encoding failed: {exc}. "
                "It will be omitted from the plan.",
                RuntimeWarning,
                stacklevel=2,
            )
    return component_specs


def _solve_patterns(
    app: "App",
    all_resources: dict[str, Any],
    storage_backends: dict[str, Any],
    storage_specs: dict[str, StorageSpec],
    *,
    target: str,
) -> dict[str, PatternSpec]:
    """Encode EventLog / Projection / Saga / Outbox patterns into specs.

    May mutate *storage_specs* — Projections force their target store to
    co-locate with the source.
    """
    pattern_specs: dict[str, PatternSpec] = {}
    registered_functions = _collect_function_names(app)

    for qname, obj in all_resources.items():
        pattern_meta = getattr(obj, "__skaal_pattern__", None)
        if not isinstance(pattern_meta, dict):
            continue
        ptype = pattern_meta.get("pattern_type")

        if ptype == "event-log":
            pattern_constraints = _storage_constraints_from_pattern(pattern_meta)
            try:
                backend_name, reason = select_backend(
                    qname, pattern_constraints, storage_backends, target=target
                )
            except UnsatisfiableConstraints as exc:
                warnings.warn(
                    f"EventLog {qname!r} could not be solved: {exc}. "
                    "No backing store will be provisioned.",
                    RuntimeWarning,
                    stacklevel=2,
                )
                backend_name, reason = "", str(exc)

            storage_meta = pattern_meta.get("storage", {})
            pattern_specs[qname] = PatternSpec(
                pattern_name=qname,
                pattern_type="event-log",
                backend=backend_name or None,
                reason=reason,
                config={
                    "retention": storage_meta.get("retention"),
                    "partitions": storage_meta.get("partitions"),
                    "durability": (
                        storage_meta.get("durability").value
                        if hasattr(storage_meta.get("durability"), "value")
                        else storage_meta.get("durability")
                    ),
                },
            )

        elif ptype == "projection":
            source = pattern_meta.get("source")
            target_obj = pattern_meta.get("target")
            handler = pattern_meta.get("handler")

            source_qname = _resolve_resource_qname(source, all_resources) if source else None
            target_qname = (
                _resolve_resource_qname(target_obj, all_resources) if target_obj else None
            )

            if handler and handler not in registered_functions:
                warnings.warn(
                    f"Projection {qname!r} references unknown handler {handler!r}. "
                    "Make sure it is registered via @app.function.",
                    RuntimeWarning,
                    stacklevel=2,
                )

            # Projections force the target store to co-locate with the source.
            if target_qname and source_qname and target_qname in storage_specs:
                existing = storage_specs[target_qname]
                storage_specs[target_qname] = existing.model_copy(
                    update={"collocate_with": source_qname}
                )

            consistency = pattern_meta.get("consistency")
            pattern_specs[qname] = PatternSpec(
                pattern_name=qname,
                pattern_type="projection",
                backend=None,
                reason=(
                    f"projection {qname!r}: {source_qname!r} → {target_qname!r} "
                    f"via handler={handler!r}"
                ),
                config={
                    "source": source_qname,
                    "target": target_qname,
                    "handler": handler,
                    "consistency": (
                        consistency.value
                        if consistency is not None and hasattr(consistency, "value")
                        else consistency
                    ),
                    "checkpoint_every": pattern_meta.get("checkpoint_every"),
                    "strict": pattern_meta.get("strict", False),
                },
            )

        elif ptype == "saga":
            steps = pattern_meta.get("steps", [])
            missing: list[str] = []
            for step in steps:
                fn_name = step.get("function")
                comp_name = step.get("compensate")
                if fn_name and fn_name not in registered_functions:
                    missing.append(f"function={fn_name!r}")
                if comp_name and comp_name not in registered_functions:
                    missing.append(f"compensate={comp_name!r}")
            if missing:
                warnings.warn(
                    f"Saga {qname!r} references unregistered names: {', '.join(missing)}. "
                    "Register them via @app.function before deploying.",
                    RuntimeWarning,
                    stacklevel=2,
                )

            pattern_specs[qname] = PatternSpec(
                pattern_name=qname,
                pattern_type="saga",
                backend=None,
                reason=(
                    f"saga {pattern_meta.get('name')!r}: {len(steps)} step(s), "
                    f"coordination={pattern_meta.get('coordination')!r}"
                ),
                config={
                    "name": pattern_meta.get("name"),
                    "steps": steps,
                    "coordination": pattern_meta.get("coordination"),
                    "timeout_ms": pattern_meta.get("timeout_ms"),
                    "missing_references": missing,
                },
            )

        elif ptype == "outbox":
            channel_obj = pattern_meta.get("channel")
            storage_obj = pattern_meta.get("storage")
            channel_qname = (
                _resolve_resource_qname(channel_obj, all_resources) if channel_obj else None
            )
            storage_qname = (
                _resolve_resource_qname(storage_obj, all_resources) if storage_obj else None
            )

            # The outbox table must live on the same backend as the primary
            # storage so the write is transactional.  Borrow its backend.
            outbox_backend: str | None = None
            if storage_qname and storage_qname in storage_specs:
                outbox_backend = storage_specs[storage_qname].backend

            pattern_specs[qname] = PatternSpec(
                pattern_name=qname,
                pattern_type="outbox",
                backend=outbox_backend,
                reason=(
                    f"outbox: writes to {storage_qname!r}, forwards to {channel_qname!r}, "
                    f"delivery={pattern_meta.get('delivery')!r}"
                ),
                config={
                    "channel": channel_qname,
                    "storage": storage_qname,
                    "delivery": pattern_meta.get("delivery"),
                },
            )
    return pattern_specs


def _resolve_resource_order(app: "App") -> list[str]:
    """Compute the topological order over the app's resource graph.

    Falls back to alphabetical order with a ``RuntimeWarning`` on cycles.
    """
    graph = build_graph(app)
    try:
        return graph.topological_order()
    except CyclicDependencyError as exc:
        warnings.warn(
            f"Cyclic dependency detected in resource graph: {exc}. "
            "Falling back to unordered resource list.",
            RuntimeWarning,
            stacklevel=2,
        )
        return sorted(app._collect_all().keys())


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

    # Build the dependency graph once up front so every sub-solver can consult
    # it.  The ordering is written into the plan so deploy generators can
    # provision resources in dependency order.
    resource_order = _resolve_resource_order(app)

    storage_specs = _solve_storage(all_resources, storage_backends, target=target)
    compute_specs = _solve_compute(all_resources, compute_backends, target=target)
    component_specs = _solve_components(app, catalog, target=target)
    # Patterns may rewrite storage_specs (Projections force collocation).
    pattern_specs = _solve_patterns(
        app, all_resources, storage_backends, storage_specs, target=target
    )

    # Flatten transitive collocate_with chains in topological order.
    _propagate_collocation(storage_specs, compute_specs, resource_order)

    # Target-level deploy config — read deploy params for the target compute
    # backend (e.g. Lambda, Cloud Run) from the catalog.  The solver doesn't
    # use these; deploy generators do.
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
        patterns=pattern_specs,
        resource_order=resource_order,
    )


def _propagate_collocation(
    storage_specs: dict[str, StorageSpec],
    compute_specs: dict[str, ComputeSpec],
    order: list[str],
) -> None:
    """
    Walk the resource graph in topological order and flatten transitive
    ``collocate_with`` chains.

    If ``A → B → C`` (A depends on B depends on C), rewrite A's
    ``collocate_with`` to point to the root of its chain (C), so deploy
    generators can group co-located resources without walking the graph.
    """

    def _root(qname: str, seen: set[str]) -> str:
        if qname in seen:  # cycle guard, the graph builder has already warned
            return qname
        seen.add(qname)
        nxt: str | None = None
        if qname in storage_specs:
            nxt = storage_specs[qname].collocate_with
        elif qname in compute_specs:
            nxt = compute_specs[qname].collocate_with
        if nxt is None or nxt == qname:
            return qname
        return _root(nxt, seen)

    for qname in order:
        if qname in storage_specs and storage_specs[qname].collocate_with:
            root = _root(storage_specs[qname].collocate_with or "", set())
            if root and root != storage_specs[qname].collocate_with:
                storage_specs[qname] = storage_specs[qname].model_copy(
                    update={"collocate_with": root}
                )
        if qname in compute_specs and compute_specs[qname].collocate_with:
            root = _root(compute_specs[qname].collocate_with or "", set())
            if root and root != compute_specs[qname].collocate_with:
                compute_specs[qname] = compute_specs[qname].model_copy(
                    update={"collocate_with": root}
                )
