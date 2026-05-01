"""
Skaal — Python API.

In-process equivalents of every ``skaal`` CLI verb.  These functions are what
the CLI dispatches to; you can call them directly from scripts, notebooks, and
test code without spawning a subprocess.

Every entry point accepts either a live :class:`~skaal.app.App` instance or a
``"module:variable"`` reference, and returns a typed Python object
(:class:`~skaal.plan.PlanFile`, list of paths, dict of outputs, …).  The API
functions raise standard Python exceptions on failure — they never print or
call :func:`sys.exit`.

Example::

    import asyncio
    import skaal
    from skaal import api

    app = skaal.App("my-service")
    # … register storage + functions …

    plan = api.plan(app, target="aws", catalog="catalogs/aws.toml")
    artifacts = api.build(plan, output_dir="artifacts", region="us-east-1")
    outputs = api.deploy("artifacts", stack="dev", region="us-east-1")

    # Or run the app locally (blocking):
    api.run(app)

    # …inside an existing event loop, use the async variant:
    asyncio.run(api.serve_async(app))
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Literal, Union

from skaal.plan import PLAN_FILE_NAME, ComputeSpec, PlanFile, StorageSpec
from skaal.settings import SkaalSettings
from skaal.types import StackProfile

if TYPE_CHECKING:
    from skaal.app import App
    from skaal.catalog.models import Catalog
    from skaal.migrate.engine import MigrationStage, MigrationState

__all__ = [
    # Types
    "AppRef",
    "PlanDiff",
    "PlanDiffEntry",
    "InfraStatus",
    "MigrationInfo",
    # App resolution
    "load_app",
    "resolve_app",
    # Verbs
    "plan",
    "build",
    "deploy",
    "run",
    "serve_async",
    "build_runtime",
    "catalog",
    "diff",
    "infra_status",
    "infra_cleanup",
    "migrate_start",
    "migrate_advance",
    "migrate_rollback",
    "migrate_status",
    "migrate_list",
]

# ── Types ──────────────────────────────────────────────────────────────────────

#: A reference to a Skaal application — either a live ``App`` instance or a
#: ``"module:variable"`` string pointing at one.
AppRef = Union["App", str]


@dataclass(frozen=True)
class PlanDiffEntry:
    """One line of a :class:`PlanDiff`."""

    name: str
    change: Literal["added", "removed", "modified"]
    before: str | None
    after: str | None


@dataclass(frozen=True)
class PlanDiff:
    """Structured diff between two :class:`~skaal.plan.PlanFile` instances."""

    old: PlanFile
    new: PlanFile
    storage: list[PlanDiffEntry] = field(default_factory=list)
    compute: list[PlanDiffEntry] = field(default_factory=list)
    components: list[PlanDiffEntry] = field(default_factory=list)
    patterns: list[PlanDiffEntry] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return (
            bool(self.storage) or bool(self.compute) or bool(self.components) or bool(self.patterns)
        )


@dataclass(frozen=True)
class MigrationInfo:
    """Summary of an in-progress migration for a single variable."""

    variable_name: str
    source_backend: str
    target_backend: str
    stage: MigrationStage
    stage_name: str


@dataclass(frozen=True)
class InfraStatus:
    """Snapshot of the resources in a :class:`~skaal.plan.PlanFile`.

    Returned by :func:`infra_status`.  Includes the raw plan plus a map of any
    currently-active migrations, so callers do not have to re-query
    :class:`~skaal.migrate.engine.MigrationEngine` themselves.
    """

    plan: PlanFile
    migrations: dict[str, MigrationInfo] = field(default_factory=dict)

    @property
    def app_name(self) -> str:
        return self.plan.app_name

    @property
    def version(self) -> int:
        return self.plan.version

    @property
    def deploy_target(self) -> str:
        return self.plan.deploy_target

    @property
    def storage(self) -> dict[str, StorageSpec]:
        return self.plan.storage

    @property
    def compute(self) -> dict[str, ComputeSpec]:
        return self.plan.compute


# ── App resolution ────────────────────────────────────────────────────────────


def _import_from_ref(module_app: str) -> "App":
    """Import ``module:variable`` and return the attribute."""
    if ":" not in module_app:
        raise ValueError(f"Expected 'module:variable', got {module_app!r}")

    module_path, _, var_name = module_app.partition(":")

    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(f"Cannot import {module_path!r}: {exc}") from exc

    obj = getattr(module, var_name, None)
    if obj is None:
        raise AttributeError(f"{module_path!r} has no attribute {var_name!r}")

    return obj


def load_app(module_app: str) -> "App":
    """Import and return the Skaal App object from a ``"module:variable"`` string.

    Adds the current directory to ``sys.path`` so bare module names resolve.

    Raises:
        ValueError:           If *module_app* does not contain a ``:``.
        ModuleNotFoundError:  If the module cannot be imported.
        AttributeError:       If the variable does not exist on the module.
    """
    return _import_from_ref(module_app)


def resolve_app(ref: AppRef) -> "App":
    """Normalise an :data:`AppRef` to a live :class:`~skaal.app.App` instance.

    Accepts either a ``"module:variable"`` string or an already-constructed
    :class:`~skaal.app.App` / :class:`~skaal.module.Module`, and returns the
    instance unchanged in the latter case.
    """
    from skaal.module import Module

    if isinstance(ref, Module):
        return ref
    if isinstance(ref, str):
        return load_app(ref)
    raise TypeError(
        f"App reference must be an App instance or 'module:variable' string, "
        f"got {type(ref).__name__}"
    )


def _split_ref(ref: AppRef) -> tuple[str, str]:
    """Return ``(source_module, app_var)`` for writing into a plan file.

    When *ref* is an :class:`App` instance there is no module reference, so
    both elements default to empty strings (the caller can fill them in later).
    """
    if isinstance(ref, str) and ":" in ref:
        module_path, _, var_name = ref.partition(":")
        return module_path, var_name or "app"
    return "", "app"


# ── catalog ───────────────────────────────────────────────────────────────────


def catalog(
    path: Path | str | None = None,
    *,
    target: str | None = None,
) -> "Catalog":
    """Load a catalog TOML and return a typed
    :class:`~skaal.catalog.models.Catalog`.

    Equivalent to ``skaal catalog`` (without the pretty-printing — the return
    value can be introspected programmatically).

    Args:
        path:   Explicit path to a catalog TOML file.  If given, *target* is
                ignored.
        target: Deploy target name (``"aws"``, ``"gcp"``, …) used to pick the
                default catalog when *path* is None.

    Raises:
        FileNotFoundError: If no catalog can be found.
        ValueError:        If the catalog file has an invalid structure.
    """
    from skaal.catalog.loader import load_typed_catalog

    return load_typed_catalog(path, target=target)


# ── plan ──────────────────────────────────────────────────────────────────────


def plan(
    app: AppRef,
    *,
    target: str | None = None,
    catalog: Path | str | None = None,  # noqa: A002 — mirrors the CLI flag
    write: bool = True,
    output_path: Path | str | None = None,
) -> PlanFile:
    """Solve infrastructure constraints and return the resulting plan.

    Equivalent to ``skaal plan``.  By default the plan is written to
    ``plan.skaal.lock`` in the current directory; pass ``write=False`` to
    compute it without touching disk.

    Args:
        app:         Either a live :class:`~skaal.app.App` or a
                     ``"module:variable"`` string.
        target:      Deploy target (``"aws"``, ``"gcp"``, ``"local"``).  Falls
                     back to the resolved ``SkaalSettings.target``.
        catalog:     Path to the catalog TOML.  Falls back to
                     ``SkaalSettings.catalog`` or the default search order
                     implemented by :func:`skaal.catalog.loader.load_catalog`.
        write:       If True (default) write ``plan.skaal.lock``.
        output_path: Custom path for the written lock file.

    Returns:
        The solved :class:`~skaal.plan.PlanFile`.

    Raises:
        FileNotFoundError: If the catalog cannot be found.
        skaal.solver.storage.UnsatisfiableConstraints:
            If the solver cannot satisfy all constraints.
    """
    from skaal.catalog.loader import load_catalog as _load_catalog
    from skaal.solver.solver import solve

    cfg = SkaalSettings()
    resolved_target = target or cfg.target
    resolved_catalog = catalog if catalog is not None else cfg.catalog

    skaal_app = resolve_app(app)
    module_path, var_name = _split_ref(app)

    raw_catalog = _load_catalog(resolved_catalog, target=resolved_target)
    plan_file = solve(skaal_app, raw_catalog, target=resolved_target)

    # Record source location so `skaal build` can re-import the app.
    plan_file.source_module = module_path
    plan_file.app_var = var_name

    if write:
        path = Path(output_path) if output_path is not None else None
        plan_file.write(path)

    return plan_file


# ── build ─────────────────────────────────────────────────────────────────────


def _coerce_plan(value: PlanFile | Path | str | None) -> PlanFile:
    """Return a :class:`PlanFile` from a plan object, a path, or the default."""
    if isinstance(value, PlanFile):
        return value
    path = Path(value) if value is not None else Path(PLAN_FILE_NAME)
    if not path.exists():
        raise FileNotFoundError(
            f"Plan file not found at {path}. " "Run `skaal.api.plan(app, target=...)` first."
        )
    return PlanFile.read(path)


def build(
    plan: PlanFile | Path | str | None = None,  # noqa: A002 — mirrors the CLI
    *,
    app: AppRef | None = None,
    output_dir: Path | str | None = None,
    region: str | None = None,
    stack: str | None = None,
    dev: bool = False,
) -> list[Path]:
    """Generate deployment artifacts from a solved plan.

    Equivalent to ``skaal build``.  Reads ``plan.skaal.lock`` by default; pass
    a :class:`~skaal.plan.PlanFile` or an explicit path to override.

    Args:
        plan:       The plan to build from.  Defaults to reading
                    ``plan.skaal.lock``.
        app:        Override the app reference baked into the plan.  Useful in
                    tests or when calling from in-process code where there is
                    no ``source_module``.
        output_dir: Directory to write artifacts into.  Falls back to
                    ``SkaalSettings.out``.
        region:     Cloud region override.  Falls back to
                    ``SkaalSettings.region``.
        stack:      Stack profile to resolve settings against.  Falls back to
                    ``SkaalSettings.stack``.
        dev:        Bundle the local ``skaal`` source into the artifact
                    (local target only).

    Returns:
        The list of generated files.

    Raises:
        FileNotFoundError: If the plan file cannot be read.
        ValueError:        If the plan references an unknown deploy target or
                           has no source module and *app* was not provided.
    """
    from skaal.deploy import get_target

    cfg = SkaalSettings().for_stack(stack)
    resolved_out = Path(output_dir) if output_dir is not None else cfg.out
    resolved_region = region or cfg.region
    stack_profile = _build_stack_profile(cfg)

    plan_file = _coerce_plan(plan)

    if app is not None:
        skaal_app = resolve_app(app)
        source_module, app_var = _split_ref(app)
        if source_module:
            plan_file.source_module = source_module
            plan_file.app_var = app_var
    else:
        if not plan_file.source_module:
            raise ValueError(
                f"{PLAN_FILE_NAME} is missing source_module — pass app= explicitly "
                "or re-run plan() to regenerate it."
            )
        skaal_app = load_app(f"{plan_file.source_module}:{plan_file.app_var}")

    target_adapter = get_target(plan_file.deploy_target)

    return target_adapter.generate_artifacts(
        app=skaal_app,
        plan=plan_file,
        output_dir=resolved_out,
        source_module=plan_file.source_module,
        app_var=plan_file.app_var,
        region=resolved_region or None,
        dev=dev,
        stack_profile=stack_profile or None,
    )


def _build_stack_profile(cfg: SkaalSettings) -> StackProfile:
    """Extract the stack-profile fields that build generators consume.

    Returns only the non-empty fields so tests and generators can use the
    truthiness of the dict to decide whether to emit stack-specific config.
    """
    profile: StackProfile = {}
    if cfg.enable_mesh:
        profile["enable_mesh"] = True
    if cfg.env:
        profile["env"] = dict(cfg.env)
    if cfg.invokers:
        profile["invokers"] = list(cfg.invokers)
    if cfg.labels:
        profile["labels"] = dict(cfg.labels)
    return profile


# ── deploy ────────────────────────────────────────────────────────────────────


def deploy(
    artifacts_dir: Path | str = "artifacts",
    *,
    stack: str | None = None,
    region: str | None = None,
    gcp_project: str | None = None,
    yes: bool = True,
) -> dict[str, str]:
    """Package and deploy previously-built artifacts via Pulumi.

    Equivalent to ``skaal deploy``.  Reads ``skaal-meta.json`` from
    *artifacts_dir* to detect the target platform.

    Returns:
        Dict of Pulumi stack outputs (e.g. ``{"apiUrl": "https://..."}``).

    Raises:
        FileNotFoundError: If *artifacts_dir* does not exist or is missing
                           ``skaal-meta.json``.
        ValueError:        If the target is unknown or required settings (e.g.
                           ``gcp_project`` for GCP) are missing.
    """
    from skaal.deploy import package_and_push
    from skaal.deploy.pulumi.meta import read_meta

    base = SkaalSettings()
    resolved_dir = Path(artifacts_dir).resolve()
    if not resolved_dir.is_dir():
        raise FileNotFoundError(
            f"Artifacts directory {resolved_dir} does not exist. "
            "Run `skaal.api.build(...)` first."
        )

    meta = read_meta(resolved_dir)
    resolved_stack = stack or ("local" if meta.get("target") == "local" else base.stack)
    cfg = base.for_stack(resolved_stack)
    resolved_region = region or cfg.region
    resolved_gcp_project = gcp_project or cfg.gcp_project

    if resolved_gcp_project is None:
        target = meta.get("target")
        if target in ("gcp", "gcp-cloudrun"):
            known = sorted(base.stacks)
            hint = (
                f" Known stack profiles: {known}."
                if known
                else " Declare one under [tool.skaal.stacks.<name>]."
            )
            raise ValueError(
                f"GCP project is required for stack {resolved_stack!r} but none was resolved.\n"
                f"  Pass --gcp-project, set SKAAL_GCP_PROJECT, or add "
                f'`gcp_project = "..."` under [tool.skaal.stacks.{resolved_stack}].{hint}'
            )

    config_overrides = _build_config_overrides(cfg, resolved_dir)

    _run_hooks(cfg.pre_deploy, cwd=resolved_dir.parent)

    outputs = package_and_push(
        artifacts_dir=resolved_dir,
        stack=resolved_stack,
        region=resolved_region,
        gcp_project=resolved_gcp_project,
        yes=yes,
        config_overrides=config_overrides or None,
    )

    _run_hooks(
        cfg.post_deploy,
        cwd=resolved_dir.parent,
        extra_env={f"SKAAL_OUTPUT_{k.upper()}": v for k, v in outputs.items()},
    )

    return outputs


def destroy(
    artifacts_dir: Path | str = "artifacts",
    *,
    stack: str | None = None,
    yes: bool = True,
) -> None:
    """Destroy previously-deployed artifacts via Pulumi.

    Equivalent to ``skaal destroy``. Reads ``skaal-meta.json`` from
    *artifacts_dir* to detect the target platform.

    Raises:
        FileNotFoundError: If *artifacts_dir* does not exist or is missing
                           ``skaal-meta.json``.
        ValueError:        If the target is unknown.
    """
    from skaal.deploy import destroy_stack
    from skaal.deploy.pulumi.meta import read_meta

    base = SkaalSettings()
    resolved_dir = Path(artifacts_dir).resolve()
    if not resolved_dir.is_dir():
        raise FileNotFoundError(
            f"Artifacts directory {resolved_dir} does not exist. "
            "Run `skaal.api.build(...)` first."
        )

    meta = read_meta(resolved_dir)
    resolved_stack = stack or ("local" if meta.get("target") == "local" else base.stack)

    destroy_stack(
        artifacts_dir=resolved_dir,
        stack=resolved_stack,
        yes=yes,
    )


def _run_hooks(
    commands: list[list[str]],
    *,
    cwd: Path,
    extra_env: dict[str, str] | None = None,
) -> None:
    """Run each argv in *commands* sequentially with :mod:`subprocess`.

    Raises :class:`subprocess.CalledProcessError` on the first failure so a
    failing pre-deploy hook aborts the deploy before it touches Pulumi, and a
    failing post-deploy hook surfaces as a non-zero exit from ``skaal deploy``.
    """
    import os
    import subprocess

    if not commands:
        return

    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    for argv in commands:
        if not argv:
            continue
        subprocess.run(argv, cwd=cwd, env=env, check=True)


def _build_config_overrides(
    cfg: SkaalSettings,
    artifacts_dir: Path,
) -> dict[str, str]:
    """Merge raw ``overrides`` with the ``deletion_protection`` shortcut.

    The shortcut expands to ``sqlDeletionProtection<Class>`` for every
    ``cloud-sql-postgres`` storage in the plan, skipping any key already
    present in ``overrides`` so explicit entries win.
    """
    merged: dict[str, str] = {
        k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in cfg.overrides.items()
    }

    if cfg.deletion_protection is not None:
        plan_path = artifacts_dir.parent / PLAN_FILE_NAME
        if plan_path.exists():
            try:
                plan_file = PlanFile.read(plan_path)
            except Exception:  # noqa: BLE001 — a broken plan shouldn't block deploy
                plan_file = None
            if plan_file is not None:
                flag = "true" if cfg.deletion_protection else "false"
                for qname, spec in plan_file.storage.items():
                    if spec.backend != "cloud-sql-postgres":
                        continue
                    class_name = qname.split(".")[-1]
                    key = f"sqlDeletionProtection{class_name}"
                    merged.setdefault(key, flag)

    return merged


# ── run / serve ───────────────────────────────────────────────────────────────


def build_runtime(
    app: AppRef,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    redis: str | None = None,
    persist: bool = False,
    db: str | Path = "skaal_local.db",
    distributed: bool = False,
    node_id: str = "node-0",
) -> Any:
    """Construct a runtime for *app*.

    Returns a :class:`~skaal.runtime.local.LocalRuntime` by default, or a
    :class:`~skaal.runtime.mesh_runtime.MeshRuntime` when *distributed* is
    ``True`` (requires ``skaal[mesh]``).

    Mirrors the options of :func:`run` but does not start the HTTP server,
    so callers can attach middleware, inspect the backends dict, or invoke
    ``serve()`` / ``shutdown()`` themselves.
    """
    skaal_app = resolve_app(app)

    if distributed:
        from skaal.runtime.mesh_runtime import MeshRuntime

        return MeshRuntime(skaal_app, host=host, port=port, node_id=node_id)

    from skaal.runtime.local import LocalRuntime

    if redis:
        return LocalRuntime.from_redis(skaal_app, redis_url=redis, host=host, port=port)
    if persist:
        return LocalRuntime.from_sqlite(skaal_app, db_path=db, host=host, port=port)
    return LocalRuntime(skaal_app, host=host, port=port)


async def serve_async(
    app: AppRef,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    redis: str | None = None,
    persist: bool = False,
    db: str | Path = "skaal_local.db",
    distributed: bool = False,
    node_id: str = "node-0",
) -> None:
    """Async variant of :func:`run` — await inside an existing event loop."""
    runtime = build_runtime(
        app,
        host=host,
        port=port,
        redis=redis,
        persist=persist,
        db=db,
        distributed=distributed,
        node_id=node_id,
    )
    await runtime.serve()


def run(
    app: AppRef,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    redis: str | None = None,
    persist: bool = False,
    db: str | Path = "skaal_local.db",
    distributed: bool = False,
    node_id: str = "node-0",
) -> None:
    """Run a Skaal app locally, blocking until the server is stopped.

    Equivalent to ``skaal run``.  Starts a minimal asyncio HTTP server where
    every ``@app.function()`` becomes a ``POST /{name}`` endpoint.

    Use :func:`serve_async` inside an already-running event loop, or
    :func:`build_runtime` to construct a runtime without starting it.
    """
    try:
        asyncio.run(
            serve_async(
                app,
                host=host,
                port=port,
                redis=redis,
                persist=persist,
                db=db,
                distributed=distributed,
                node_id=node_id,
            )
        )
    except KeyboardInterrupt:
        return


# ── diff ──────────────────────────────────────────────────────────────────────


def _diff_specs(
    old: dict[str, Any],
    new: dict[str, Any],
    attr: str,
) -> list[PlanDiffEntry]:
    """Compute a structured diff of two spec dicts keyed by qualified name."""
    entries: list[PlanDiffEntry] = []
    old_keys = set(old)
    new_keys = set(new)

    for name in sorted(new_keys - old_keys):
        entries.append(
            PlanDiffEntry(
                name=name,
                change="added",
                before=None,
                after=getattr(new[name], attr),
            )
        )
    for name in sorted(old_keys - new_keys):
        entries.append(
            PlanDiffEntry(
                name=name,
                change="removed",
                before=getattr(old[name], attr),
                after=None,
            )
        )
    for name in sorted(old_keys & new_keys):
        before = getattr(old[name], attr)
        after = getattr(new[name], attr)
        if before != after:
            entries.append(
                PlanDiffEntry(
                    name=name,
                    change="modified",
                    before=before,
                    after=after,
                )
            )
    return entries


def diff(
    new_plan: PlanFile | Path | str | None = None,
    *,
    old_plan: PlanFile | Path | str | None = None,
    app: AppRef | None = None,
    catalog: Path | str | None = None,  # noqa: A002 — mirrors the CLI flag
) -> PlanDiff:
    """Diff two plans, or a stored plan against a freshly-solved one.

    Equivalent to ``skaal diff``.  Four calling conventions:

    - ``diff()``                   — load ``plan.skaal.lock`` as both sides (empty diff).
    - ``diff(new_plan)``           — diff ``plan.skaal.lock`` → *new_plan*.
    - ``diff(app=app)``            — re-solve *app* and diff against ``plan.skaal.lock``.
    - ``diff(new, old_plan=old)``  — diff two explicit plans.

    Args:
        new_plan: The "after" plan (or its path).  If omitted and *app* is
                  given, the plan is produced by re-solving in-memory
                  (no file is written).
        old_plan: The "before" plan.  Defaults to ``plan.skaal.lock``.
        app:      Re-solve this app to produce *new_plan*.  Ignored if
                  *new_plan* is also given.
        catalog:  Catalog path to use when re-solving *app*.

    Returns:
        A :class:`PlanDiff`.
    """
    old = _coerce_plan(old_plan)

    if new_plan is None and app is not None:
        new = plan(
            app,
            target=old.deploy_target,
            catalog=catalog,
            write=False,
        )
    elif new_plan is not None:
        new = _coerce_plan(new_plan)
    else:
        # No new side supplied — return an empty diff (parity with `skaal diff`
        # called with no module:app, which just pretty-prints the existing plan).
        new = old

    return PlanDiff(
        old=old,
        new=new,
        storage=_diff_specs(old.storage, new.storage, "backend"),
        compute=_diff_specs(old.compute, new.compute, "instance_type"),
        components=_diff_specs(old.components, new.components, "implementation"),
        patterns=_diff_specs(old.patterns, new.patterns, "backend"),
    )


# ── infra ─────────────────────────────────────────────────────────────────────


def _collect_migrations(plan_file: PlanFile) -> dict[str, MigrationInfo]:
    """Scan ``.skaal/migrations/<app>`` for in-progress migrations."""
    from skaal.migrate.engine import MigrationEngine

    migrations: dict[str, MigrationInfo] = {}
    for name, spec in plan_file.storage.items():
        if not spec.previous_backend or spec.previous_backend == spec.backend:
            continue
        try:
            engine = MigrationEngine(plan_file.app_name, name)
            state = engine.load_state()
        except Exception:  # noqa: BLE001 — filesystem errors are non-fatal
            continue
        if state is None:
            continue
        migrations[name] = MigrationInfo(
            variable_name=state.variable_name,
            source_backend=state.source_backend,
            target_backend=state.target_backend,
            stage=state.stage,
            stage_name=state.stage.name.lower(),
        )
    return migrations


def infra_status(
    plan: PlanFile | Path | str = PLAN_FILE_NAME,  # noqa: A002
) -> InfraStatus:
    """Return the current infrastructure state from a plan file.

    Equivalent to ``skaal infra status``.
    """
    plan_file = _coerce_plan(plan)
    migrations = _collect_migrations(plan_file)
    return InfraStatus(plan=plan_file, migrations=migrations)


def infra_cleanup(variable: str, *, app_name: str | None = None) -> bool:
    """Remove migration state for *variable*.

    Equivalent to ``skaal infra cleanup``.  Returns True if a state file was
    present and removed, False if no migration was recorded for this variable.
    """
    from skaal.migrate.engine import MigrationEngine

    resolved_app = app_name or _current_app_name()
    engine = MigrationEngine(resolved_app, variable)
    state_path = engine._state_path
    if state_path.exists():
        state_path.unlink()
        return True
    return False


# ── migrate ───────────────────────────────────────────────────────────────────


def _current_app_name() -> str:
    """Return the app name from ``plan.skaal.lock``, falling back to cwd name."""
    plan_path = Path(PLAN_FILE_NAME)
    if plan_path.exists():
        try:
            return PlanFile.read(plan_path).app_name
        except Exception:  # noqa: BLE001
            pass
    return Path.cwd().name


def migrate_start(
    variable: str,
    from_backend: str,
    to_backend: str,
    *,
    app_name: str | None = None,
) -> "MigrationState":
    """Start a new 6-stage migration for *variable*.

    Raises:
        RuntimeError: If a migration is already in progress for *variable*.
    """
    from skaal.migrate.engine import MigrationEngine, MigrationStage

    resolved_app = app_name or _current_app_name()
    engine = MigrationEngine(resolved_app, variable)
    existing = engine.load_state()
    if existing is not None and existing.stage != MigrationStage.DONE:
        raise RuntimeError(
            f"Migration for {variable!r} already in progress "
            f"(stage {existing.stage}: {existing.stage.name.lower()})."
        )
    return engine.start(from_backend, to_backend)


def migrate_advance(
    variable: str,
    *,
    app_name: str | None = None,
) -> "MigrationState":
    """Advance the migration for *variable* to the next stage."""
    from skaal.migrate.engine import MigrationEngine

    resolved_app = app_name or _current_app_name()
    engine = MigrationEngine(resolved_app, variable)
    state = engine.load_state()
    if state is None:
        raise RuntimeError(
            f"No migration in progress for {variable!r}. Call migrate_start() first."
        )
    return engine.advance(state)


def migrate_rollback(
    variable: str,
    *,
    app_name: str | None = None,
) -> "MigrationState":
    """Roll the migration for *variable* back one stage."""
    from skaal.migrate.engine import MigrationEngine

    resolved_app = app_name or _current_app_name()
    engine = MigrationEngine(resolved_app, variable)
    state = engine.load_state()
    if state is None:
        raise RuntimeError(f"No migration in progress for {variable!r}.")
    return engine.rollback(state)


def migrate_status(
    variable: str,
    *,
    app_name: str | None = None,
) -> "MigrationState | None":
    """Return the current migration state for *variable*, or ``None`` if idle."""
    from skaal.migrate.engine import MigrationEngine

    resolved_app = app_name or _current_app_name()
    engine = MigrationEngine(resolved_app, variable)
    return engine.load_state()


def migrate_list(app_name: str | None = None) -> list["MigrationState"]:
    """Return every in-progress migration (optionally filtered to one app)."""
    from skaal.migrate.engine import MigrationEngine, MigrationState

    if app_name is not None:
        return MigrationEngine(app_name, "").list_all()

    base_dir = Path(".skaal/migrations")
    if not base_dir.exists():
        return []

    states: list[MigrationState] = []
    for app_dir in sorted(p for p in base_dir.iterdir() if p.is_dir()):
        for path in sorted(app_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text())
                states.append(MigrationState(**data))
            except Exception:  # noqa: BLE001
                continue
    return states


# ── Helpers exposed for the CLI layer ─────────────────────────────────────────


def iter_plan_files(root: Path | None = None) -> Iterable[Path]:
    """Yield every ``plan.skaal.lock`` file found under *root*.

    Convenience helper for tooling built on top of the Python API.
    """
    base = Path(root) if root is not None else Path.cwd()
    yield from base.rglob(PLAN_FILE_NAME)
