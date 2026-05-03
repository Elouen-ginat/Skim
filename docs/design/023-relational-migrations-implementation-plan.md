# ADR 023 — Relational Migrations Beyond `create_all`

**Status:** Implemented
**Date:** 2026-05-02
**Related:** [user_gaps.md §B.3](../user_gaps.md#b3-relational-tier-skaalrelationalpy), [user_gaps.md "Top of list" #6](../user_gaps.md), [skaal/relational.py](../../skaal/relational.py), [skaal/backends/sqlite_backend.py](../../skaal/backends/sqlite_backend.py), [skaal/backends/postgres_backend.py](../../skaal/backends/postgres_backend.py), [skaal/migrate/engine.py](../../skaal/migrate/engine.py), [skaal/cli/migrate_cmd.py](../../skaal/cli/migrate_cmd.py), [ADR 004](004-six-stage-migration.md), [ADR 007](007-schema-versioning.md)

## Goal

Give Skaal users a first-class story for evolving the relational schema of their `@app.storage(kind="relational")` SQLModels past `create_all`: versioned, reviewable, rollback-capable migrations that the runtime can apply automatically in dev and that operators can stage explicitly in prod.

This pass closes user-gaps "Top of list" item **#6** ("Relational migrations beyond `create_all`", §B.3) — the next ranked **P0** item without an implementation plan after items #1 (blob, ADR 016), #2 (agent persistence, ADR 018), #3 (`skaal init` + dev, ADR 020), #4 (solver diagnostics, ADR 021), and #5 (catalog overrides, ADR 022).

## Why this is next

The §B.3 gap is the first thing a real team hits the day after their second deploy. Today `skaal/relational.py:81-84` calls `await backend.ensure_relational_schema(model_cls)`, which both `SqliteBackend` (`sqlite_backend.py:339-344`) and `PostgresBackend` (`postgres_backend.py:420-425`) implement as `metadata.create_all`. That covers "first deploy ever" and nothing else: no version table, no diff, no `ALTER`, no rollback, and no signal when the in-code model has drifted from the deployed schema. ADR 004's six-stage flow and ADR 007's `__skaal_version__` are about *data* migration (KV-store backend swap, model-shape upgrade-on-read) — neither knows what `ALTER TABLE` is.

This is also a coherent cut:

- SQLAlchemy is already a transitive dependency (via `sqlmodel>=0.0.22`), so Alembic adds no new heavyweight surface.
- Both built-in relational backends already expose an `AsyncEngine` (`sqlite_backend.py:81-93`, `postgres_backend.py:108-120`); Alembic can plug straight into it.
- The `skaal migrate` CLI surface and the `MigrationEngine` JSON-state convention (`.skaal/migrations/{app_name}/...`) are already in place and only need a sub-namespace.
- `@app.storage(kind="relational")` already validates SQLModel table classes (`skaal/relational.py:27-35`), so the model-discovery side is solved.

The remaining work is wiring, not invention.

Items #7 (secrets, P0), #10 (per-row TTL, P0), and #8 (examples + testing, P1) are intentionally out of scope: each is its own design discussion with its own runtime/deploy entanglements, and none is mechanically reachable from this pass.

## Scope

This pass includes:

- An **Alembic-backed** relational migration system, owned and orchestrated by Skaal — users never run `alembic` directly.
- A `RelationalMigrationStore` per app that holds the versions directory and a generated `env.py`, lives under `.skaal/migrations/{app_name}/relational/`, and is gitignored by default with an opt-in to commit (the recommended path).
- A `MigrationKind` enum that namespaces existing 6-stage *data* migrations from new *relational* schema migrations under one CLI verb.
- New CLI subcommands under `skaal migrate relational ...`: `autogenerate`, `upgrade`, `downgrade`, `current`, `history`, `stamp`, `check`.
- A `--dry-run` flag on `upgrade`/`downgrade` that prints the SQL Alembic would emit without executing it (closes the related §A.7 P1 — "no `--dry-run`").
- Auto-apply on local runtime startup behind an opt-out: `LocalRuntime` runs `upgrade head` on first boot for the dev backend; production targets do not. Mirrors `ensure_schema`'s current behavior so existing local apps keep working.
- New typed objects in `skaal/types/relational.py`: `RelationalRevision`, `RelationalMigrationPlan`, `RelationalMigrationStep`, `RelationalMigrationStatus`. These are the public shapes returned by the Python API.
- Python API surface in `skaal/api.py`: `relational_autogenerate`, `relational_upgrade`, `relational_downgrade`, `relational_current`, `relational_history`, `relational_check` — each returns one of the typed objects above.
- Integration with `skaal init`: the starter project's `pyproject.toml` gains a hint comment, and `.gitignore` excludes `.skaal/migrations/*/relational/__pycache__` while keeping `versions/` checked in.
- Backend support for SQLite (`aiosqlite`) and Postgres (`asyncpg`) — the only two relational backends that exist today.
- Tests: end-to-end against SQLite, a Postgres-shaped offline `--sql` mode test, plus unit tests for the typed objects, autodetect filter, and CLI plumbing.

This pass does **not** include:

- DynamoDB or Firestore "schema" migrations — neither is a relational backend; both stay on the data-migration path.
- Online/zero-downtime migrations of the "rename column without breaking readers" variety. That is the relational analog of ADR 004's six-stage flow and deserves its own ADR; the current pass only provides the linear `up`/`down` baseline that flow would later sit on top of.
- Multi-tenant per-tenant migration runners.
- Cross-database transactional migrations.
- Read-replica failover during migration.
- A web UI for migration status.

## Code unification opportunity

This is the single most useful unification opportunity in the codebase right now, and it is the reason this ADR is worth writing instead of just "drop in Alembic":

Skaal already has **three things called "migration"**:

1. `skaal/migrate/engine.py` — the 6-stage backend swap (KV data movement). State key: a single variable name. Persisted at `.skaal/migrations/{app}/{variable}.json`. CLI: `skaal migrate {start,advance,rollback,status,list}`.
2. `skaal/types/schema.py` — Pydantic model versioning (`__skaal_version__` + `@migrate_from`). State key: model class. Persisted in the stored row itself (`__skaal_version__: N`). No CLI surface.
3. *(missing today)* — Relational DDL versioning. State key: revision id. Persisted in an `alembic_version` table.

These solve different problems and should stay separate engines. But the **user surface** ("how do I evolve my schema?") deserves to be unified. The plan unifies in three places:

### Unification 1 — `MigrationKind` enum and CLI verb

Introduce one new enum in `skaal/migrate/engine.py`:

```python
class MigrationKind(StrEnum):
    DATA = "data"            # 6-stage backend swap (existing)
    RELATIONAL = "relational"  # Alembic DDL upgrade/downgrade (new)
    MODEL = "model"          # __skaal_version__ row migrations (existing, future CLI)
```

`skaal migrate` becomes a sub-app with three sub-apps:

```
skaal migrate data start --variable counter.Counts --from local --to dynamodb
skaal migrate data advance --variable counter.Counts
skaal migrate data status --variable counter.Counts

skaal migrate relational autogenerate -m "add user.full_name"
skaal migrate relational upgrade [REV|head] [--dry-run]
skaal migrate relational downgrade [REV|-1]
skaal migrate relational current
skaal migrate relational history
skaal migrate relational check               # exit 1 if model drift detected
skaal migrate relational stamp REV
```

The bare `skaal migrate {start,advance,rollback,status,list}` keeps working as deprecated shims that delegate to `skaal migrate data ...`. Two-version deprecation window before removal — same shape ADR 022 used for catalog short-name compatibility.

This costs one file rename inside the `cli/` layer (the existing `migrate_cmd.py` becomes `cli/migrate/data_cmd.py`, and `cli/migrate/__init__.py` becomes the parent typer app) and zero functional changes to the existing 6-stage code.

### Unification 2 — `MigrationState` location convention

The existing per-variable JSON state lives at `.skaal/migrations/{app_name}/{variable}.json`. Relational state should follow the same naming so an operator who knows where to look for one knows where to look for the other:

```
.skaal/migrations/{app_name}/
    data/                                # ← rehome existing JSON files here
        counter__Counts.json
    relational/
        env.py                           # generated
        alembic.ini                      # generated
        versions/
            20260502_1234_add_user.py
```

`MigrationEngine.STATE_DIR` (`engine.py:55`) becomes `.skaal/migrations/{app_name}/data/`. Existing state files are moved on first run by a one-shot `_migrate_legacy_state_dir()` helper that runs idempotently inside `MigrationEngine.__init__`. Old paths under `.skaal/migrations/{app_name}/{variable}.json` are migrated to the `data/` subdirectory and deleted; the helper logs at INFO once. Greenfield apps never see the legacy layout.

### Unification 3 — `ensure_schema` stays on the public API but delegates

`skaal.ensure_relational_schema(model_cls)` (re-exported as `from skaal import ensure_relational_schema`, `__init__.py:33`) keeps its public signature. Its body changes from "call backend.ensure_relational_schema" (which calls `metadata.create_all`) to:

1. If a `versions/` directory exists with at least one revision, run `relational_upgrade(model_cls, "head")`.
2. Otherwise, fall back to `metadata.create_all` as today.

That keeps the local-runtime auto-apply behavior identical for projects that have not yet created a migration, and makes the moment a user runs `skaal migrate relational autogenerate` for the first time the same moment the runtime starts driving versioning.

The `metadata.create_all` path inside `SqliteBackend.ensure_relational_schema` and `PostgresBackend.ensure_relational_schema` is the implementation detail that gets shared with the new code path. Both backends gain a small public method `relational_engine(model_cls) -> AsyncEngine` (returns `self._engine` after `_ensure_relational_engine`) so the migration runner does not need to import either backend class to plug into Alembic.

## Design

### Resolution pipeline

```
user invokes `skaal migrate relational upgrade head`
        ↓
api.relational_upgrade(app_ref, target="head")
        ↓
load app, resolve relational backend(s) per registered model
        ↓
RelationalMigrationStore.ensure(app_name)
   - creates .skaal/migrations/{app}/relational/{env.py, alembic.ini, versions/}
   - env.py uses Skaal's resolved AsyncEngine, not a DSN from an external file
        ↓
Alembic runtime (programmatic API: alembic.config.Config + command.upgrade)
   - includes the SQLModel.metadata of every registered relational model
        ↓
   if --dry-run: capture SQL via offline mode (`--sql`) into RelationalMigrationPlan
   else:        run upgrade against the live engine
        ↓
return RelationalMigrationPlan | RelationalMigrationStatus
```

The store ensures atomicity by using Alembic's own `alembic_version` table — Skaal does not invent a parallel ledger, because then the two could disagree.

### Multiple backends in one app

A single Skaal app may have several `@app.storage(kind="relational")` classes that resolve to **different** backends (e.g. one local SQLite, one Postgres) when the planner runs. Two reasonable behaviors exist; the plan picks the second:

1. *One global `versions/` directory*, single linear history. Simple, but breaks the moment two backends drift apart (e.g. SQLite gets `JSON` columns Postgres needs `JSONB` for, or one backend skips a revision).
2. **One `versions/` directory per resolved backend.** The store layout becomes `.skaal/migrations/{app}/relational/{backend_name}/{env.py, alembic.ini, versions/}`. Each registered relational backend gets its own linear history; `skaal migrate relational upgrade` iterates over them in registration order. This is what Alembic itself recommends for multi-database setups.

`backend_name` here is the resolved backend label from the plan (e.g. `sqlite`, `postgres-aurora`), not the model qualified name. Models that resolve to the same backend share a history.

The CLI takes an optional `--backend NAME` flag to scope an operation; with no flag, it operates on every registered relational backend in dependency order.

### Autogenerate

`alembic revision --autogenerate -m "msg"` is run programmatically. Skaal's `env.py` template builds `target_metadata` as the union of every SQLModel registered to that backend in the app:

```python
target_metadata = sqlmodel.SQLModel.metadata  # all registered tables
```

Filtering: any table whose name does not appear in the registered models is excluded via Alembic's `include_object` callback. This prevents Alembic from emitting a `DROP TABLE` for, say, the `skaal_kv` table that the KV facade uses against the same Postgres backend (`postgres_backend.py:85-92`). The `include_object` predicate is part of the generated `env.py` and its allowlist is computed from `app._collect_all()` at run time.

Without this filter, `autogenerate` against Postgres would emit `DROP TABLE skaal_kv` on every run because the KV table is not declared in any SQLModel. This is the single subtle correctness gate of the design and is covered by a dedicated test (see Test plan below).

### `check` — drift detection

`skaal migrate relational check` is the CI surface. It runs Alembic's autogenerate in offline mode against the live database and asserts the produced revision is empty. Exit `0` on no drift, `1` on drift, `2` on connection / config error (consistent with ADR 021's solver UNSAT exit code). The drift report is a `RelationalMigrationPlan` object with `is_empty=False`.

### Auto-apply on `LocalRuntime`

The current `_patch_storage` flow (`runtime/base.py:74-92`) calls `wire_relational_model` and then leaves schema creation to the first call into `open_relational_session` (which calls `ensure_relational_schema` again — double-call but idempotent). The new flow:

1. `wire_relational_model(obj, backend)` (unchanged).
2. **NEW**: `await ensure_schema(obj)` is invoked once per model at startup. The implementation now branches:
   - If `versions/` exists for the resolved backend → `relational_upgrade(model_cls, "head")`.
   - Else → `metadata.create_all` (existing behavior).
3. Runtime startup fails fast if `upgrade` raises; today silent SQL exceptions on first call are a frequent debugging cost.

For production targets (deploy-time codegen), no auto-apply happens. Operators run `skaal migrate relational upgrade head` explicitly via a one-shot deploy job. The deploy generators (`skaal/deploy/templates/`) gain a `migrate.sh` script template under the AWS and GCP targets that runs the upgrade via `skaal migrate relational upgrade head`. Wiring `migrate.sh` into the actual deploy pipeline is an explicit operator step in this pass; first-class deploy integration is a follow-up ADR (it overlaps with secrets, ADR-pending #7).

## New types — `skaal/types/relational.py`

The plan adds **one** new types module. All shapes are frozen dataclasses, consistent with `Page`/`SecondaryIndex` in `types/storage.py:9-21` and `CandidateReport`/`Diagnosis` in `types/solver.py`. They are re-exported from `skaal.types`.

```python
# skaal/types/relational.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal


class RelationalMigrationOp(StrEnum):
    """One DDL operation in a planned upgrade or downgrade."""
    CREATE_TABLE = "create_table"
    DROP_TABLE = "drop_table"
    ADD_COLUMN = "add_column"
    DROP_COLUMN = "drop_column"
    ALTER_COLUMN = "alter_column"
    CREATE_INDEX = "create_index"
    DROP_INDEX = "drop_index"
    OTHER = "other"           # raw SQL or anything Alembic emits we don't classify


@dataclass(frozen=True)
class RelationalMigrationStep:
    """One row in a dry-run plan."""
    op: RelationalMigrationOp
    table: str | None
    detail: str               # human-readable description
    sql: str                  # the rendered SQL Alembic would emit


@dataclass(frozen=True)
class RelationalRevision:
    """One Alembic revision present in `versions/`."""
    revision_id: str          # Alembic short hash, e.g. "20260502_1234_add_user"
    down_revision: str | None
    message: str
    created_at: datetime
    is_head: bool             # True if this is the current head in versions/
    is_applied: bool          # True if the live DB has run this revision


@dataclass(frozen=True)
class RelationalMigrationPlan:
    """Output of `upgrade --dry-run` / `downgrade --dry-run` / `check`."""
    backend_name: str
    direction: Literal["upgrade", "downgrade"]
    from_revision: str | None     # current DB head; None if alembic_version is empty
    to_revision: str              # target — "head", "base", or a revision id
    steps: list[RelationalMigrationStep] = field(default_factory=list)
    is_empty: bool = False        # True if no DDL would be emitted (used by `check`)


@dataclass(frozen=True)
class RelationalMigrationStatus:
    """Output of `current` and post-execution result of `upgrade`/`downgrade`."""
    backend_name: str
    current_revision: str | None  # None if alembic_version table is empty
    head_revision: str | None     # None if versions/ is empty
    pending: list[RelationalRevision] = field(default_factory=list)
    applied: list[RelationalRevision] = field(default_factory=list)

    @property
    def is_at_head(self) -> bool:
        return self.current_revision == self.head_revision and self.head_revision is not None
```

Re-export site: `skaal/types/__init__.py` imports the four dataclasses + the enum, adds them to `__all__` under a new `# relational migrations` section. No existing type changes.

`MigrationKind` lives at `skaal/migrate/engine.py` rather than in `types/`, mirroring `MigrationStage`'s placement: it is part of the engine's vocabulary, not a public-facing shape.

## Files touched

### New
- `skaal/types/relational.py` — the four typed objects + `RelationalMigrationOp`.
- `skaal/migrate/relational.py` — `RelationalMigrationStore`, the Alembic wrapper. Public functions: `autogenerate`, `upgrade`, `downgrade`, `current`, `history`, `check`, `stamp`. Each returns one of the new types.
- `skaal/migrate/_alembic_env.py.tmpl` — Jinja-free string template for the generated `env.py`. Kept inline in code rather than under `skaal/deploy/templates/` because it is generated into a user-owned directory, not a deployable artifact.
- `skaal/cli/migrate/__init__.py` — parent `typer` app for `skaal migrate`.
- `skaal/cli/migrate/data_cmd.py` — moved content of the old `skaal/cli/migrate_cmd.py`; commands stay byte-for-byte identical.
- `skaal/cli/migrate/relational_cmd.py` — new sub-app for the `relational` verb.
- `tests/migrate/relational/test_autogenerate.py`
- `tests/migrate/relational/test_upgrade_downgrade.py`
- `tests/migrate/relational/test_check.py`
- `tests/migrate/relational/test_include_object_filter.py` — guards the `skaal_kv`-not-dropped invariant.
- `tests/migrate/relational/test_legacy_state_dir.py` — covers the one-shot `data/` rehome.
- `tests/types/test_relational_types.py`

### Removed
- `skaal/cli/migrate_cmd.py` — content moved into `skaal/cli/migrate/data_cmd.py`. The shim aliasing `skaal migrate start` etc. lives in `skaal/cli/migrate/__init__.py`.

### Modified
- `skaal/migrate/engine.py` — add `MigrationKind` enum (StrEnum); change `STATE_DIR` to `.skaal/migrations/{app}/data/`; add `_migrate_legacy_state_dir()` helper.
- `skaal/migrate/__init__.py` — export `MigrationKind` and the new relational module's public functions.
- `skaal/migrate/shadow.py` — no functional change; one comment update referencing the new layout.
- `skaal/relational.py` — rewrite `ensure_schema` to dispatch to `relational.upgrade(model, "head")` if `versions/` exists, else fall back to `backend.ensure_relational_schema`. Add `current_head(model_cls)` helper.
- `skaal/backends/sqlite_backend.py` — add `relational_engine() -> AsyncEngine`.
- `skaal/backends/postgres_backend.py` — add `relational_engine() -> AsyncEngine`.
- `skaal/api.py` — add `relational_autogenerate`, `relational_upgrade`, `relational_downgrade`, `relational_current`, `relational_history`, `relational_check` to `__all__` and as thin wrappers over `skaal.migrate.relational`.
- `skaal/__init__.py` — export the same six API functions and the `RelationalMigration*` types.
- `skaal/cli/main.py` — `migrate_app` import path changes from `skaal.cli.migrate_cmd` to `skaal.cli.migrate`. No registration change.
- `skaal/cli/init_cmd.py` (or wherever the starter template lives) — gitignore `.skaal/migrations/*/relational/__pycache__`; commit `versions/`.
- `pyproject.toml` — add `alembic>=1.13` to base `dependencies`. Sized at ~600 KB on PyPI; SQLAlchemy is already pulled in transitively. Putting it in an optional extra would be wrong because `ensure_schema` is on the base public API and would fail at import time without it.
- `docs/user_gaps.md` — strike `Relational migrations beyond create_all` from the "Top of list" once this lands; update §B.3.
- `CLAUDE.md` — one-line addition under "Quick Reference Commands": `skaal migrate relational autogenerate -m "msg"`.

## CLI changes — backward compatibility

| Today                            | After this pass                                  | Compat?                                                 |
| -------------------------------- | ------------------------------------------------ | ------------------------------------------------------- |
| `skaal migrate start ...`        | `skaal migrate data start ...`                   | Old form keeps working with a `DeprecationWarning`.     |
| `skaal migrate advance ...`      | `skaal migrate data advance ...`                 | Same.                                                   |
| `skaal migrate rollback ...`     | `skaal migrate data rollback ...`                | Same.                                                   |
| `skaal migrate status ...`       | `skaal migrate data status ...`                  | Same.                                                   |
| `skaal migrate list`             | `skaal migrate data list`                        | Same.                                                   |
| *(none)*                         | `skaal migrate relational ...`                   | New.                                                    |
| Python: `api.migrate_start(...)` | unchanged                                        | Backward-compatible alias for `api.data_migrate_start`. |
| Python: `ensure_relational_schema(model)` | unchanged signature; new behavior          | Backward-compatible.                                    |

The shim is one Typer callback per old command that prints a one-line warning to stderr and forwards `**kwargs` to the new sub-app. Two-version deprecation window.

## Test plan

Follows the per-area `tests/<area>/` structure already in use (see `tests/schema/`, `tests/storage/`, `tests/cli/`).

1. **`tests/types/test_relational_types.py`** — instantiate every dataclass, assert frozen/hashable where claimed, assert `RelationalMigrationStatus.is_at_head` corner cases.
2. **`tests/migrate/relational/test_autogenerate.py`** — register two SQLModels, call `relational_autogenerate(app, "init")`, assert one revision file exists with both `create_table` ops in `upgrade()` and matching `drop_table` ops in `downgrade()`.
3. **`tests/migrate/relational/test_upgrade_downgrade.py`** — autogenerate two revisions, run upgrade head, assert `current_revision == head`. Run `downgrade("-1")`, assert previous head. Run `upgrade("head")`, assert idempotent (no-op).
4. **`tests/migrate/relational/test_check.py`** — at head, `check` returns `is_empty=True` and exits 0. Add a column to the SQLModel without generating a revision, `check` returns `is_empty=False` with one `add_column` step and exits 1.
5. **`tests/migrate/relational/test_include_object_filter.py`** — register a SQLModel against a Postgres-shaped engine (use `aiosqlite` with a manually-created `skaal_kv` table to simulate the KV facade table), run `autogenerate`, assert the produced revision contains **no** `drop_table('skaal_kv')`. This is the correctness-critical test.
6. **`tests/migrate/relational/test_legacy_state_dir.py`** — pre-create `.skaal/migrations/myapp/foo.json`, instantiate `MigrationEngine("myapp", "foo")`, assert the file moves to `.skaal/migrations/myapp/data/foo.json` and the helper is a no-op on second call.
7. **`tests/cli/test_migrate_relational_cmd.py`** — invoke each CLI subcommand via Typer's `CliRunner`, assert exit codes and JSON output where applicable.
8. **`tests/cli/test_migrate_data_cmd_compat.py`** — invoke `skaal migrate start --variable v --from a --to b`, assert it still works and prints a `DeprecationWarning` to stderr.

Postgres tests gate on `pg_isready` env detection (same pattern as `tests/storage/`); if absent, fall back to `--sql` offline mode and assert the rendered SQL string.

## Sequence

1. Land `skaal/types/relational.py` + `tests/types/test_relational_types.py`. Pure-data, no integration. Mergeable on its own.
2. Add `MigrationKind`, `relational_engine()` accessors, and the legacy-state-dir migrator. Tests #6 land here.
3. Add `skaal/migrate/relational.py` + the `_alembic_env` template. Tests #2, #3, #5 land here.
4. Wire the CLI sub-app, add the deprecation shims. Tests #7, #8 land here.
5. Rewrite `skaal/relational.ensure_schema` to dispatch. Test #4 lands here. Update §B.3 in `docs/user_gaps.md` and add `alembic>=1.13` to `pyproject.toml`.
6. Update `skaal init` template + `CLAUDE.md` quick reference.

Each step is a ~200–400 LOC PR. The plan picks the smallest cuttable PR boundaries that keep `make test` green at every step.

## Open questions

- **Alembic in base deps vs optional extra?** Plan picks base. Argument for an `[migrate]` extra: keeps `pip install skaal` smaller. Argument against (and the plan's choice): `ensure_relational_schema` is part of the public API and is called by the local runtime on every startup that uses `kind="relational"`. Putting Alembic behind an extra makes the local-runtime happy path fail with `MissingExtraError` for anyone who does not know to add `[migrate]` — a worse UX than the marginal `pip install` size cost.
- **Auto-apply on every `LocalRuntime` start?** Plan says yes. An opt-out `LocalRuntime(auto_migrate=False)` is added; the env var `SKAAL_AUTO_MIGRATE=0` flips the default. Production deploys always default to off.
- **Versioning the `versions/` directory in git?** Plan says yes. The starter `.gitignore` excludes `__pycache__` only. The argument for committing it is decisive: a migration is a reviewable artifact; not committing it defeats the entire point.
- **Alembic's branch/merge support?** Out of scope. The plan validates linear history only and refuses an autogenerate that would produce a branch (Alembic itself supports this via `--head`). A future ADR can lift this.

## Consequences

**Positive:**
- Teams past first deploy get an unsurprising path — autogenerate, review, commit, deploy — that mirrors what they have used in Django, Flask-Migrate, or plain Alembic for a decade.
- `--dry-run` plus `check` give CI a way to fail PRs that drift from the deployed schema, closing one of the most common shadow-prod bugs.
- The `skaal migrate {data,relational}` split makes the CLI legible the day a team needs both kinds of migration in flight.
- The `ensure_schema` dispatch is invisible to existing local users until the moment they create their first revision, which is exactly the moment they want to start versioning.

**Negative:**
- Adds Alembic as a base dependency. ~600 KB; small in absolute terms but real.
- The `include_object` filter is the one subtle correctness gate. A regression here would emit `DROP TABLE skaal_kv` against production. The dedicated test #5 above is non-negotiable; CI must run it on every PR.
- The legacy state-dir migrator (`data/`) is durable code that exists only for backward compatibility. It earns its keep through the two-version deprecation window, then can be removed.
- Multi-backend apps need to think about per-backend revision history, which is one more concept than "one app, one history". The CLI's default of "operate on every backend in dependency order" hides this for the common case.
