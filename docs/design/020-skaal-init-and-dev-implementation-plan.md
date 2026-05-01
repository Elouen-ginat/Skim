# ADR 020 — `skaal init` and `skaal dev` Implementation Plan

**Status:** Proposed
**Date:** 2026-05-01
**Related:** [user_gaps.md §A.1](../user_gaps.md#a1-cli-zero-config-and-dev-loop), [skaal/cli/main.py](../../skaal/cli/main.py), [skaal/cli/init_cmd.py](../../skaal/cli/init_cmd.py), [skaal/cli/run_cmd.py](../../skaal/cli/run_cmd.py), [skaal/cli/_reload.py](../../skaal/cli/_reload.py)

## Goal

Close the remaining P0 adoption gap in the user-gaps report by making the first-run and local-dev story feel like a single, obvious path:

- `skaal init` creates a usable starter app
- `skaal dev` is the documented hot-reload entry point
- `skaal run` remains the lower-level runtime command

The target user flow is still the one called out in `user_gaps.md`: install Skaal, scaffold once, start a local server with reload, and iterate without reading multiple docs pages first.

## Why this is next

The top-of-list items in `docs/user_gaps.md` already have implementation plans for the two capability/correctness gaps ahead of this one:

1. blob storage — [ADR 016](016-blob-storage-tier-implementation-plan.md)
2. agent persistence — [ADR 018](018-agent-persistence-implementation-plan.md)

That leaves the adoption pass in §A.1 as the next broad, user-visible improvement. It is not a research project: most of the underlying pieces already exist, but they are still split across commands and not yet presented as the clean "happy path" promised by the report.

## Current facts

Today the repo already has part of the solution:

- `skaal init` exists and scaffolds a project root, package, `pyproject.toml`, `README.md`, `.gitignore`, and `catalogs/local.toml`.
- `skaal run` already supports hot reload through `skaal/cli/_reload.py`, including automatic reload defaults for interactive dev and `--reload-dir`.
- The CLI root already registers `init` and `run`.
- There are CLI tests for both scaffolding and reload supervision.

But the user-gap from §A.1 is still only partially closed:

- there is no first-class `skaal dev` command
- the hot-reload workflow is hidden under `skaal run` instead of being the obvious "edit/save/reload" entry point
- the scaffolded next steps still point users at `skaal run`
- the docs and help surface do not yet frame init + dev as the default onboarding path

## Scope

This pass includes:

- adding a dedicated `skaal dev` CLI command as the supported local-development entry point
- reusing the existing reload supervisor and local runtime path instead of creating a second implementation
- making `skaal dev` default to reload-on and project-root watching, while keeping explicit override flags
- updating `skaal init` output and starter docs so the next step is `skaal dev`
- exposing the `MODULE:APP` fallback and dev workflow more clearly in CLI help and user-facing docs
- extending CLI tests to cover the new command and the updated onboarding flow

This pass does **not** include:

- a new runtime server implementation
- IDE integration or editor plugins
- browser live-reload for frontend assets
- project-template variants (API app, worker app, dashboard app, etc.)
- package-manager bootstrapping beyond the current starter layout

## Decision

Add `skaal dev` as a thin, explicit development command built on top of the existing `skaal run` + reload machinery.

Do **not** fork the runtime path or create a second server stack just to support development ergonomics. The right shape is:

- one local runtime implementation
- one reload supervisor
- one scaffolding flow
- two user-facing command levels:
  - `skaal dev` for everyday local iteration
  - `skaal run` for direct/runtime-oriented usage

That keeps the adoption improvement focused on product surface and documentation rather than on duplicate execution logic.

## Command behavior

### `skaal dev`

`skaal dev` should:

- accept the same target-resolution model as `skaal run` (`MODULE:APP` argument or `[tool.skaal].app`)
- default to reload enabled
- watch the project root by default, including Python and TOML changes
- forward the existing runtime/storage flags that still matter in local development
- call into the same reload supervisor and `api.run(...)` path already used by `skaal run`

### `skaal run`

`skaal run` should remain available as the lower-level local runtime command:

- still usable without reload
- still usable for direct runtime testing and scripting
- still the shared implementation target for the dev command

The distinction is product-facing, not architectural: `dev` is the friendly entry point, `run` is the direct one.

### `skaal init`

`skaal init` should stay intentionally small:

- generate one starter package and app
- keep the local catalog bundled
- print next steps that point to editable install and `skaal dev`
- avoid template sprawl in this pass

## Files touched

- `skaal/cli/main.py` — register the new `dev` command
- `skaal/cli/dev_cmd.py` (new) — thin command surface delegating to the existing run/reload path
- `skaal/cli/run_cmd.py` — factor any shared option/dispatch helpers needed by both commands
- `skaal/cli/init_cmd.py` — update scaffolded next steps to prefer `skaal dev`
- `tests/cli/test_init_cmd.py` — assert the new onboarding output
- `tests/cli/test_run_reload.py` and/or a new `tests/cli/test_dev_cmd.py` — cover `skaal dev` behavior
- `README.md` and relevant docs pages — document the init/dev flow as the default quickstart
- `docs/user_gaps.md` — point §A.1 and the top-of-list item at this ADR

## Migration / compatibility

This is additive:

- existing `skaal run` users keep working
- existing scaffolded projects keep working
- the user-visible change is that Skaal gains a clearer default development command and updated docs/output to match

## Open questions

- whether `skaal new` should exist as an alias for `skaal init`, or stay out of scope until usage proves it is needed
- whether `skaal dev` should expose every `run` flag directly or only the local-dev subset with passthrough for advanced cases
- whether shell-completion installation should land in the same pass or immediately after this one as separate CLI polish
