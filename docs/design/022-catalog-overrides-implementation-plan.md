# ADR 022 — Catalog Overrides per Environment

**Status:** Proposed
**Date:** 2026-05-02
**Related:** [user_gaps.md §A.5](../user_gaps.md#a5-catalog-ergonomics), [skaal/catalog/loader.py](../../skaal/catalog/loader.py), [skaal/catalog/models.py](../../skaal/catalog/models.py), [skaal/cli/catalog_cmd.py](../../skaal/cli/catalog_cmd.py), [skaal/settings.py](../../skaal/settings.py), [ADR 003](003-catalog-toml-format.md)

## Goal

Let a team keep one base catalog and overlay per-environment differences (dev / staging / prod) without copy-pasting whole TOML files. Two surfaces:

- **`extends = "..."` in any catalog file** — declare a parent; Skaal resolves the chain and deep-merges before the typed `Catalog.from_raw` validation runs.
- **`skaal catalog validate <path>`** — run the same validation that `load_typed_catalog` runs today and print a per-section pass/fail report, so users discover required fields without running `skaal plan`.

This pass closes user-gaps item **#5** ("Catalog overrides per environment", §A.5) — the next ranked item without an implementation plan after items #1 (blob, ADR 016), #2 (agent persistence, ADR 018), #3 (`skaal init` + dev, ADR 020), and #4 (solver diagnostics, ADR 021).

## Why this is next

The §A.5 gap is the first non-P0 to hit users repeatedly past the prototype stage. Items #6–#10 either need bigger design discussions (`relational migrations beyond create_all`, `secrets`, `per-row TTL`) or are P1 polish (`backend-native cursor`, `examples ladder`). Catalog overrides are mechanical, scoped to two files (`skaal/catalog/loader.py` + `skaal/cli/catalog_cmd.py`), and lift the obvious "copy the whole TOML and edit one line" pain that every multi-environment user hits.

The companion §A.5 wins (`skaal catalog` not advertised in docs, opaque file-lookup fallback chain) come along for free in this pass: `skaal catalog validate` is the natural surface to advertise, and the lookup chain becomes useful once `extends` makes "where does this value come from?" a real question.

## Scope

This pass includes:

- An `[skaal] extends = "..."` field in any catalog TOML (relative path or registered short name) that references a parent catalog. Multiple chained `extends` are allowed; cycles raise `CatalogError`.
- Deep-merge semantics: child overlays replace parent values at the **per-backend** key level. So a child `[storage.sqlite] read_latency = ...` replaces the parent's whole `[storage.sqlite]` block; it does not key-merge inside it. This rule is intentionally simple — "swap a backend, add a backend" is the user-facing model, not "patch a single field on a backend."
- A `[skaal] remove = ["storage.sqlite"]` list to delete a parent backend in the child without redeclaring it. Mirrors `extends`'s simplicity and avoids the "how do I make a child *narrower*?" trap.
- A `--catalog dev` short-name resolver that maps to `catalogs/dev.toml` so the CLI reads naturally. The existing `[project.entry-points."skaal.catalogs"]` plugin path keeps working unchanged.
- A `skaal catalog validate <path>` CLI that loads the chain, runs the existing `Catalog.from_raw` validation, and prints `OK` / per-error report. Exit code `2` on validation failure (consistent with ADR 021's solver UNSAT exit code).
- Settings extension: `SkaalSettings.catalog` (already added in ADR 020) feeds the same resolver. No new env var beyond `SKAAL_CATALOG`.
- New typed object `CatalogSource` in `skaal/types/catalog.py` carrying `(path, raw, parent: CatalogSource | None)` so the resolved chain is introspectable from tests and from `skaal catalog --explain` (next pass).
- Tests covering: chain resolution, cycle detection, child-replaces-backend semantics, `remove` semantics, short-name resolution, `validate` happy path + structured-error path, and that `load_typed_catalog` returns the same object whether the chain is one file or three.

This pass does **not** include:

- Per-field merge inside a single backend block. If users need it later, the `remove` mechanic + redeclaration covers most cases; field-level merging adds a "did this win or lose?" debugging cliff that is not worth its weight today.
- A `skaal catalog explain Storage.Profiles` command. That belongs with §A.6 (Plan/lock readability) — separate pass, P2.
- A multi-stack registry (`catalogs/{dev,staging,prod}.toml`) bundled in `skaal init`. The starter template can adopt this once the surface stabilizes; out of scope here.
- JSON-Schema generation / publication of the catalog format. The typed Pydantic models already act as a schema for code consumers; `skaal catalog validate` is the user-facing equivalent. JSON Schema is a separate ask if it surfaces.
- Remote/HTTP catalog sources. `extends` resolves filesystem paths and registered short names only.

## Design

### Resolution pipeline

`load_catalog(path, target)` (`skaal/catalog/loader.py:141`) currently returns the raw TOML dict. The change inserts one step:

```
_resolve_path(path, target) → concrete file or bundled dict
        ↓
read TOML, parse                ← unchanged
        ↓
NEW: _resolve_extends(raw, base_path) → flat dict
        ↓
return raw                      ← unchanged
```

`_resolve_extends`:

1. If `raw["skaal"]["extends"]` is absent, return `raw` minus the `[skaal]` table.
2. Resolve the parent via the same `_resolve_path` helper (path or short name) anchored at `base_path.parent`.
3. Recurse on the parent first (depth-first). Detect cycles by tracking visited absolute paths; raise `CatalogError("circular extends: a.toml → b.toml → a.toml")`.
4. Deep-merge: for each top-level section (`storage`, `compute`, `network`), child entries replace parent entries by backend name. Top-level keys outside the known sections are passed through with child winning.
5. Apply `raw["skaal"]["remove"]`: a list of dotted paths (`"storage.sqlite"`, `"compute.t3-micro"`) deleted post-merge. Removing an absent key is a no-op (warn-log only — not an error, because parent chains move).

Both `extends` and `remove` live under a single reserved `[skaal]` table so user-defined `[storage.skaal]` etc. cannot collide.

### `skaal catalog validate`

`skaal/cli/catalog_cmd.py` gains a sub-app, not a flag, so it can grow:

```
skaal catalog                          # (existing) browse backends
skaal catalog validate [PATH]          # NEW
skaal catalog sources [PATH]           # NEW — print the resolved chain
```

`validate`:

1. Resolve the catalog via `load_catalog` (which now applies extends).
2. Call `Catalog.from_raw(...)` (`skaal/catalog/models.py:78`) — the existing validator already checks `[storage.X.deploy]`, `[storage.X.wire]`, etc.
3. On success print `OK ({path}) — N storage, M compute, K network backends`.
4. On `ValueError` / `CatalogError` print the structured report through `rich` (one block per failing section), exit code 2.

`sources` is a one-screen helper: it prints the resolved `CatalogSource` chain depth-first, so users can see "this `read_latency` came from `dev.toml`, which extended `base.toml`." Cheap to add and removes the "where does this value come from?" friction the `extends` mechanic introduces.

### Types

New file `skaal/types/catalog.py`:

```python
@dataclass(frozen=True)
class CatalogSource:
    path: Path                 # absolute, resolved
    raw: dict[str, Any]        # TOML contents minus [skaal] table
    parent: "CatalogSource | None"
    removes: tuple[str, ...] = ()  # dotted paths slated for deletion
```

Re-exported from `skaal.types`. Makes the resolved chain a value object so tests can assert against it without re-parsing. The existing `Catalog` Pydantic model (`skaal/catalog/models.py`) is unchanged — `CatalogSource` lives one level below it, in the loader's pre-validation layer.

### Error wrapping

Cycle detection, missing parent, and validation failure all raise `CatalogError` (already exists in `skaal/errors.py` after ADR 021 fixed the `SkaalConfigError` definition). The CLI `_errors.cli_error_boundary` already handles `SkaalError` → exit code; `CatalogError` inherits exit code `1` by default — bump validation failures to `2` via a per-call `typer.Exit(2)` so they line up with solver UNSAT.

### `skaal init` template impact

`skaal init` ships a single `catalogs/local.toml` today. This pass does **not** change that template — adding a `[skaal] extends = ...` line by default would set up an inheritance chain users haven't asked for. Instead, document the pattern in the new `docs/catalogs.md` page so users can opt in once they actually have multiple environments.

## Files touched

- `skaal/catalog/loader.py` — `_resolve_extends`, `_apply_removes`, `_build_source_chain` helpers; `load_catalog` and `load_typed_catalog` consume them. `load_catalog_with_sources(path) -> tuple[dict, CatalogSource]` (new) for callers that need the chain.
- `skaal/types/catalog.py` (new) — `CatalogSource` dataclass.
- `skaal/types/__init__.py` — export `CatalogSource`.
- `skaal/errors.py` — no new classes; `CatalogError` already covers the failure mode.
- `skaal/cli/catalog_cmd.py` — switch the existing `catalog` callback to a `typer.Typer()` sub-app; add `validate` and `sources` subcommands.
- `tests/catalog/test_extends.py` (new) — chain resolution, deep-merge semantics, cycle detection, `remove` semantics, short-name parents.
- `tests/catalog/test_validate_cmd.py` (new) — `skaal catalog validate` happy path + structured-error path + exit code 2.
- `tests/catalog/test_sources_cmd.py` (new) — `skaal catalog sources` prints the chain.
- `docs/catalogs.md` (new short page) — inheritance, override semantics, validate command, common patterns (`base.toml` + `dev.toml` + `prod.toml`).
- `docs/user_gaps.md` — point §A.5 and the top-of-list item at this ADR.

## Migration / compatibility

Purely additive at the file format level: catalogs without `[skaal] extends` or `[skaal] remove` parse identically to today. The new `[skaal]` table is reserved — any existing catalog that happens to define `[storage.skaal]` etc. is unaffected because the reserved table is `[skaal]` at the top level, not nested inside a section.

`load_catalog(path, target)` keeps its current signature and return type. `load_catalog_with_sources(...)` is added alongside.

`skaal catalog` becomes a sub-app, but the bare `skaal catalog` invocation continues to print the backend tables — Typer attaches the existing callback to the sub-app's default command. Existing CLI users see no change.

## Open questions

- **Bundled-catalog parents.** Should `extends = "aws"` (short name) resolve through `_BUNDLED_CATALOGS` so users can `extends = "aws"` in a project-local override? First cut allows it — the resolver already uses `_resolve_path` which knows about bundled. Risk is "I extended aws.toml, then upgraded Skaal, and the parent changed under me." Mitigated by `skaal catalog sources` showing the resolved path so the issue is visible.
- **`extends` chains across short-name registries.** If two installed plugins register the same short name, the existing `_resolve_path` resolution rule wins. This pass does not change that behavior; document it.
- **Per-field merging.** Deferred deliberately. If real users hit a case where "I want to nudge `read_latency.max` on the parent's sqlite without redeclaring the rest of the spec," we add a `[skaal.merge]` opt-in mode rather than changing the default semantics.
- **`skaal catalog validate` against a chain that includes the bundled catalog.** Should it validate the parent too, or just the resolved merge? First cut validates the merge — that is the only object the solver actually consumes. Validating the parent in isolation belongs to a future `skaal catalog lint --strict`.
- **Plan-time provenance.** Once `CatalogSource` is in place, `plan.skaal.lock` could record which file each backend choice was *defined in*. Not in this pass; logged as a follow-up so `skaal diff` can surface "the dev catalog changed read_latency."
