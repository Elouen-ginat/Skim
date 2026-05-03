# ADR 021 — Solver Diagnostics: UNSAT Explanations and Constraint Suggestions

**Status:** Proposed
**Date:** 2026-05-01
**Related:** [user_gaps.md §A.4](../user_gaps.md#a4-error-messages-and-validation), [skaal/solver/storage.py](../../skaal/solver/storage.py), [skaal/solver/explain.py](../../skaal/solver/explain.py), [skaal/cli/_errors.py](../../skaal/cli/_errors.py), [skaal/types/constraints.py](../../skaal/types/constraints.py), [ADR 002](002-z3-backend-selection.md)

## Goal

When `skaal plan` cannot satisfy the declared constraints, surface a message that names the resource, lists every candidate backend with the specific constraint each one violated, and — when a single relax of a numeric constraint would have made one of them feasible — suggests it. Stop showing first-time users a Z3 stack trace and a one-line "No backend satisfies …" sentence.

This pass closes user-gaps item **#4** ("Solver-failure error messages with closest-match suggestions", §A.4) — the highest-ranked remaining adoption-blocking P0 after items #1 (blob, ADR 016), #2 (agent persistence, ADR 018), and #3 (`skaal init` + hot-reload, commit `ba9860d`).

It also picks up the smaller §A.4 wins that fall out for free once the diagnostic surface exists: a "did you mean…" message on bad `AccessPattern("badvalue")` enums, a "install the `vector` extra" hint on optional-dep `ImportError`s, and Skaal-shaped wrapping of `tomllib.TOMLDecodeError` for catalogs.

## Why this is next

The §A "ergonomics" P0s are now down to one: solver-error UX. Items #1–#3 above unblocked first-run; the next thing a user does is declare a constraint that doesn't fit the local catalog and hit `skaal plan`. Today they get this:

```
Cannot satisfy constraints for 'Profiles'. No backend satisfies: read_latency < 1ms, durability=durable
```

— with no indication of which backends were considered, what each one offered, or what the user could change. `_build_error_reasons` (`skaal/solver/storage.py:126`) lists the *requested* constraints, not the gap. Z3 has the information; it is just not extracted.

This is the smallest-scope P0 left in §A: a single solver-side change, a single CLI-side change, a few rendering helpers. No new dependencies. No new runtime surface. The `explain.py` module already shows the codebase has a place for this kind of formatting work.

The companion §A.4 items (`AccessPattern` enum suggestions, missing-extra hints, TOML wrapping) are bundled here because they are one-line `__init_subclass__` hooks and a single `try/except` in the catalog loader, and §A.4 explicitly calls them out as P1 follow-ons of the same surface.

## Scope

This pass includes:

- A `Diagnosis` data class in `skaal/solver/diagnostics.py` carrying: `variable_name`, `requested: dict[str, Any]`, `candidates: list[CandidateReport]`. Each `CandidateReport` carries `backend_name`, `display_name`, and `violations: list[Violation]`. Each `Violation` names the constraint key, the requested value, the offered value (or "not supported"), and a `slack` field for numeric constraints (how far off, in the constraint's native unit).
- A pre-flight pass in `select_backend` that, before invoking Z3, evaluates each candidate against each constraint via the existing `_CONSTRAINT_CHECKERS` registry. The result feeds both the success path (no change to solver behavior) and the UNSAT diagnostic.
- A "closest-match" rule: the candidate(s) with the smallest weighted slack across numeric constraints, breaking ties on cost. The rule is intentionally simple — pick from the actual candidate set, do not synthesize a hypothetical backend.
- A "single-relax suggestion" rule: if relaxing exactly one numeric constraint to the closest candidate's offered value would make it feasible, emit a one-line suggestion ("If `read_latency` were `< 5ms` instead of `< 1ms`, `sqlite` would satisfy.").
- `UnsatisfiableConstraints` carries the `Diagnosis` object as `.diagnosis` (in addition to the current short-string `__str__` for back-compat).
- A `render_diagnosis(d: Diagnosis, *, rich: bool = False) -> str` in `skaal/solver/explain.py` that produces the user-visible block. The CLI error boundary calls it for `UnsatisfiableConstraints` and prints the result through `rich.console.Console` (already a top-level dep).
- `AccessPattern`, `Durability`, `Consistency`: a small `_invalid_value(cls, value)` helper that raises `ValueError(f"{value!r} is not a valid {cls.__name__}. Did you mean {nearest!r}? Valid values: …")` using `difflib.get_close_matches`. Added once on a shared base class so it covers all enums in `skaal/types/constraints.py`.
- Decorator-time `ImportError` wrapping for optional extras. `@app.vector(...)` already raises `ImportError` from inside the decorator body; wrap with `SkaalError("vector tier needs the optional dep: pip install \"skaal[vector]\"")`. Mirror for any other optional-extra-gated decorator we currently surface.
- Catalog `tomllib.TOMLDecodeError` wrapping in `skaal/catalog/loader.py`: re-raise as `SkaalError(f"{path}: invalid TOML at line {err.lineno}: {err.msg}")`.
- Tests for: an UNSAT plan produces a `Diagnosis` with at least one candidate per backend in the catalog; the "single-relax" suggestion fires for the obvious case; the "did you mean" message fires for `AccessPattern("rand-read")`; missing `skaal[vector]` raises `SkaalError`, not bare `ImportError`; bad TOML names the file and line.

This pass does **not** include:

- Z3 unsat-core extraction. Z3 can produce an unsat core, but it is over the *encoded* boolean variables, not the user's constraint vocabulary, and translating it back is more work than this pass justifies. The pre-flight per-candidate evaluation produces a strictly more useful message because it speaks the user's language.
- Multi-relax suggestions ("if you relaxed both X and Y…"). Combinatorial; a single-relax suggestion covers the common case.
- An interactive `skaal plan --explain` walkthrough. That belongs with the §A.6 Plan/lock readability work (separate, P2).
- Re-formatting the *successful* plan output. `_print_plan_table` and `explain_plan` already serve that case.
- Renaming or breaking `UnsatisfiableConstraints`. Adding `.diagnosis` is additive; the existing constructor signature is preserved.

## Design

### Pre-flight evaluation

Today `select_backend` (`skaal/solver/storage.py`) builds a Z3 model and either returns a selection or raises `UnsatisfiableConstraints` with a string-only reason. The change:

```python
def select_backend(...):
    candidates = _evaluate_candidates(constraints, backends)   # NEW
    # candidates: list[CandidateReport] — one per backend with all violations

    feasible = [c for c in candidates if not c.violations]
    if not feasible:
        diag = _diagnose(variable_name, constraints, candidates)
        raise UnsatisfiableConstraints(variable_name, diagnosis=diag)

    # ... existing Z3 minimization over `feasible` for cost ...
```

`_evaluate_candidates` reuses `_CONSTRAINT_CHECKERS` so there is one source of truth for "does spec X satisfy constraint Y?" — same logic feeds Z3 (via boolean clauses) and the diagnostic. Numeric constraints additionally compute a `slack`:

- `read_latency < 1ms` against `spec.read_latency.max = 5ms` → `slack = +4ms` (offered − requested).
- `size_hint = 100GB` against `spec.max_size_gb = 50` → `slack = -50GB`.
- Categorical constraints (`durability`, `kind`, `access_pattern`, `consistency`, `residency`) have no slack — `Violation.slack = None`. The rendering distinguishes "not supported" from "supported with X delta."

The pre-flight is cheap: at most a few dozen candidates × ~10 constraints. It runs unconditionally; the cost is negligible compared to a Z3 solve.

### Closest-match selection

Implemented as a stable sort on `(num_violations, weighted_slack, cost)`. Weighted slack normalizes each numeric dimension to the constraint's request (so 1ms-over-1ms is not drowned out by 50GB-over-100GB). The "winner" is the head of the sorted list.

A `single-relax` suggestion is emitted iff the closest match has exactly one violation and that violation has a non-None `slack`. The suggestion text reads from a per-constraint formatter (already partly present in `_CONSTRAINT_FORMATTERS`). For the categorical case, no suggestion is emitted — the user has to pick a different value, and there is no defensible "do you mean."

### `Diagnosis` rendering

`render_diagnosis(d, rich=False)` produces:

```
Cannot plan storage 'Profiles'.

  Requested:
    read_latency  < 1ms
    durability    durable
    kind          kv

  Considered 3 backends; none satisfied:
    sqlite          ✗ read_latency  offered ≤ 5ms     (off by 4ms)
                    ✓ durability    durable
                    ✓ kind          kv
    local-redis     ✗ durability    offered ephemeral, persistent (no 'durable')
                    ✓ read_latency  ≤ 2ms
                    ✓ kind          kv
    local-map       ✗ read_latency  offered ≤ 0.1ms   (✓)
                    ✗ durability    offered ephemeral
                    ✓ kind          kv

  Closest match: sqlite (1 unmet constraint).
  → If you can accept read_latency < 5ms, sqlite would satisfy.
    Edit the @app.storage(read_latency=...) on Profiles, or pick a faster
    backend in your catalog.
```

Rich mode wraps backend names, ✓/✗ glyphs, and the closest-match line in colour. Plain mode (no TTY, `--no-color`, or `SKAAL_LOG_FORMAT=json`) drops glyphs for ASCII `OK`/`FAIL`.

### CLI wiring

`skaal/cli/_errors.py:cli_error_boundary` learns one new branch:

```python
except UnsatisfiableConstraints as exc:
    if exc.diagnosis is not None:
        Console().print(render_diagnosis(exc.diagnosis, rich=_is_tty()))
    else:
        _log_error(exc)
    raise typer.Exit(2) from exc          # distinct exit code from generic errors
```

Exit code `2` is allocated specifically for "constraint UNSAT" so CI scripts can tell "Skaal ran but rejected your design" from "Skaal crashed."

### Enum "did you mean" hook

`skaal/types/constraints.py` adds a `_StrictStrEnum` mixin (subclasses `str, Enum`) overriding `_missing_` to:

```python
@classmethod
def _missing_(cls, value):
    suggestions = difflib.get_close_matches(str(value), [m.value for m in cls], n=1)
    hint = f" Did you mean {suggestions[0]!r}?" if suggestions else ""
    valid = ", ".join(repr(m.value) for m in cls)
    raise ValueError(f"{value!r} is not a valid {cls.__name__}.{hint} Valid: {valid}.")
```

Applied to `Durability`, `AccessPattern`, `Consistency`. Pure additive; existing valid values continue to resolve unchanged.

### Optional-extra `ImportError` wrapping

A small `@require_extra("vector")` decorator in `skaal/errors.py` wraps decorator bodies so a missing dep raises `SkaalError("vector tier needs: pip install 'skaal[vector]'")` instead of `ImportError: No module named 'langchain_core'`. Applied to `@app.vector` and any other optional-extra entry point.

### TOML error wrapping

`skaal/catalog/loader.py` already opens TOML in a known place. Wrap that read with:

```python
try:
    data = tomllib.load(fh)
except tomllib.TOMLDecodeError as err:
    raise SkaalError(f"catalog {path}: invalid TOML at line {err.lineno}: {err.msg}") from err
```

## Files touched

- `skaal/solver/diagnostics.py` (new) — `Violation`, `CandidateReport`, `Diagnosis`, `_evaluate_candidates`, `_diagnose`, `_rank_candidates`.
- `skaal/solver/storage.py` — refactor `select_backend` to call the pre-flight; add `diagnosis` keyword to `UnsatisfiableConstraints.__init__` (back-compat default `None`).
- `skaal/solver/compute.py` — same pre-flight pattern for compute UNSAT (smaller surface, but the user's experience should be uniform across solver passes).
- `skaal/solver/explain.py` — add `render_diagnosis(d, *, rich)`; reuse the existing `h`/`dim` helpers.
- `skaal/cli/_errors.py` — add `UnsatisfiableConstraints` branch; introduce exit code 2.
- `skaal/types/constraints.py` — add `_StrictStrEnum` and apply to `Durability`, `AccessPattern`, `Consistency`.
- `skaal/errors.py` — add `require_extra(extra_name)` decorator.
- `skaal/decorators.py` — wrap `@app.vector` (and any other optional-extra-gated decorator) with `require_extra(...)`.
- `skaal/catalog/loader.py` — wrap `tomllib.load` raise.
- `tests/solver/test_diagnostics.py` (new) — UNSAT cases producing expected `Diagnosis` shape; ranking; single-relax detection; categorical no-slack handling.
- `tests/solver/test_solver.py` — keep the existing UNSAT test; add an assertion that `.diagnosis` is populated.
- `tests/cli/test_plan_cmd.py` — add a CLI-level test that an UNSAT plan exits with code 2 and prints the rendered diagnosis (asserts on the closest-match line, not the whole formatting).
- `tests/types/test_constraints.py` — `AccessPattern("rand-read")` raises with `"random-read"` in the message.
- `tests/catalog/test_loader.py` — bad TOML raises `SkaalError` naming the file and line.
- `docs/diagnostics.md` (new short page) — user-facing reading guide for the diagnostic block, including the exit-code contract.

## Migration / compatibility

`UnsatisfiableConstraints.__init__` keeps its current `(variable_name, reason="")` signature; the new `diagnosis` is keyword-only with default `None`, so any third-party code constructing or catching the exception continues to work. The `__str__` short message is unchanged for the `diagnosis=None` path. CLI users who relied on exit code `1` for solver failures should migrate to `2`; this is documented in `docs/diagnostics.md` and in the CHANGELOG.

`AccessPattern("invalid")` already raises `ValueError`; the change is the message text, not the exception type. No code that currently catches `ValueError` will newly break.

## Open questions

- **Compute and component diagnostics parity.** `select_backend` for compute is structurally similar; mirroring the diagnostic there is in scope. Components (`solver/components.py`) have a different selection model — punted unless a P0 user case surfaces.
- **Catalog drift in the hint.** A "would have satisfied" hint is only as good as the catalog. If the user's catalog is missing the obvious backend (e.g. no Redis at all in `local.toml`), the suggestion can read awkwardly ("relax read_latency to 5ms" when the user actually needs to add a backend). Acceptable in v1; a future version can detect "no candidate matches the *kind* at all" and suggest catalog edits instead.
- **JSON-mode rendering.** When `SKAAL_LOG_FORMAT=json`, structured logging skips the rendered block. The `Diagnosis` dataclass is JSON-serializable, so we emit the structured object as a single log record; CI tools can parse it. Whether to also dump the rendered text alongside is a stylistic choice — first cut emits structured only.
- **Severity of the "did you mean" suggestion.** `difflib` will sometimes suggest a wrong neighbor (e.g. `"random-write"` for `"rand"`). Acceptable cost; the message lists *all* valid values directly after the suggestion so the user is never trapped.
- **Internationalization.** The rendered diagnostic is English-only. Out of scope; revisit when the rest of the CLI is i18n'd.
