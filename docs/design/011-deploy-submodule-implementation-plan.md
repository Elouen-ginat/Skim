# ADR 011 — Deploy Submodule Implementation Plan

**Status:** Proposed
**Date:** 2026-04-29
**Related:** [ADR 010](010-deploy-submodule-refactor.md)

## Goal

Execute the `skaal.deploy` refactor as a direct cutover. The end state should be
small, typed, and internally consistent.

This plan assumes:

- no backward compatibility
- no temporary shim modules
- no duplicate object models for the same concept
- cross-module structural types live in `skaal.types.deploy`

## Current Facts

The current package already points toward the target split, but the code is
still organized around the older flat layout:

- `skaal/deploy/registry.py` contains three concrete adapters with nearly the
  same deployment pipeline.
- `skaal/deploy/push.py` mixes Pulumi CLI orchestration, metadata I/O, and
  packaging helpers.
- `skaal/deploy/local_automation.py` duplicates parts of the Pulumi setup used
  by the cloud targets.
- `skaal/deploy/aws.py`, `skaal/deploy/gcp.py`, and `skaal/deploy/local.py`
  mix artifact generation, stack construction, helper utilities, and target
  specific naming logic.
- tests currently reach deep into private deploy modules, so the refactor must
  update tests at the same time instead of preserving the old import graph.

The current CLI only imports `get_target`, so the production surface is already
small enough to cut over cleanly.

## Refactor Rules

1. Reuse existing Pydantic models from `skaal.plan` and `skaal.deploy.config`
   instead of creating deploy-only copies.
2. Add missing deploy-specific structural types to `skaal.types.deploy` and
   re-export them from `skaal.types`.
3. Keep execution-only helper objects internal to `skaal.deploy`; only shared
   structural contracts belong in `skaal.types.deploy`.
4. Prefer one object per concept: one registry adapter shape, one Pulumi runner
   contract, one metadata shape, one shared helper for each naming rule.
5. Delete legacy modules as soon as the new package layout is wired. Do not
   leave alias modules behind.

## Canonical Objects

The refactor should standardize on these owners:

| Concept | Canonical owner | Notes |
| --- | --- | --- |
| Solved application plan | `skaal.plan.PlanFile` | Includes `StorageSpec`, `ComputeSpec`, and `ComponentSpec`. |
| Deploy-time backend and compute config | `skaal.deploy.config` | Keep using Pydantic models for catalog-backed config. |
| Pulumi stack and Docker resource payloads | `skaal.types.deploy` | Existing `PulumiStack`, `PulumiResource`, `LocalServiceSpec`, and Docker TypedDicts stay here. |
| Deploy target contract | `skaal.deploy.target.DeployTarget` | Tighten signatures to remove raw `Any` and untyped dicts at module boundaries. |
| Registry strategy objects | `skaal.deploy.targets.registry` | Internal dataclasses are fine here; they are not shared outside deploy. |
| Pulumi execution contract | `skaal.deploy.pulumi` | One Protocol used by CLI-backed and Automation-backed runners. |

## Types To Add Or Normalize

Add the following deploy-specific shared types to `skaal.types.deploy`:

- `TargetName = Literal["aws", "aws-lambda", "gcp", "gcp-cloudrun", "local", "local-docker"]`
- `ConfigOverrides: TypeAlias = dict[str, str]`
- `StackOutputs: TypeAlias = dict[str, str]`
- `StackProfile` as a `TypedDict` for baked-in stack settings such as `env`,
  `invokers`, `labels`, and `enable_mesh`
- `DeployMeta` as a `TypedDict` for the serialized artifact metadata file
- `RouteSpec`, `AuthConfig`, `RateLimitConfig`, and `GatewayConfig` for the
  gateway configuration currently passed around as raw nested dicts
- `AppLike` as a small `Protocol` exposing only the attributes deploy actually
  reads from the application object
- `BackendWiring` as the named result returned by the backend wiring builder

Do not add deploy-local copies of `PlanFile`, `StorageSpec`, `ComputeSpec`,
`ComponentSpec`, or the deploy config Pydantic models. Those objects already
exist and should remain the source of truth.

## Target Package Layout

The final package layout should be:

```text
skaal/deploy/
├── __init__.py
├── config.py
├── target.py
├── backends/
│   ├── __init__.py
│   ├── deps.py
│   ├── external.py
│   ├── handler.py
│   ├── services.py
│   └── wiring.py
├── builders/
│   ├── __init__.py
│   ├── apigw.py
│   ├── aws.py
│   ├── common.py
│   ├── gcp.py
│   └── local.py
├── packaging/
│   ├── __init__.py
│   ├── aws.py
│   ├── gcp.py
│   └── local.py
├── pulumi/
│   ├── __init__.py
│   ├── automation.py
│   ├── cli.py
│   ├── env.py
│   ├── meta.py
│   └── render.py
└── targets/
    ├── __init__.py
    ├── aws.py
    ├── gcp.py
    ├── local.py
    └── registry.py
```

The top level should stay thin. Everything else moves under one of the five
responsibility packages.

## Final Internal Shape

The refactor should converge on one deploy pipeline:

`PlanFile` + `AppLike` -> builder -> artifacts -> packaging -> Pulumi runner -> outputs

Implementation rules:

- `builders/*` produce stack dictionaries and target-specific generated files.
- `packaging/*` performs packaging only; it must not select stacks, read Pulumi
  outputs, or own environment setup.
- `pulumi/*` owns stack selection, config application, up/destroy, backend
  environment, and metadata persistence.
- `targets/*` wires target-specific generation, packaging, runner selection,
  and output keys.
- `registry.py` contains one shared adapter class backed by typed strategies.

## Direct Cutover Sequence

The implementation should happen in this order.

### 1. Tighten the contracts first

- Add the new shared types to `skaal.types.deploy`.
- Re-export them from `skaal.types.__init__`.
- Update `skaal.deploy.target.DeployTarget` to use `AppLike`, `StackProfile`,
  `ConfigOverrides`, and `StackOutputs`.
- Replace unnamed tuple or raw dict returns at deploy boundaries with typed
  objects before moving files.

Exit condition: deploy module boundaries are typed well enough that file moves
do not spread new `Any`-based APIs.

### 2. Create the final package seams

- Move `_deps.py` to `backends/deps.py`.
- Move `_external.py` to `backends/external.py`.
- Split `_backends.py` into `backends/handler.py`, `backends/services.py`, and
  `backends/wiring.py`.
- Move `_render.py` to `pulumi/render.py`.
- Move `local_automation.py` to `pulumi/automation.py`.
- Split `push.py` into `pulumi/cli.py`, `pulumi/meta.py`, and `packaging/*`.

Do not leave forwarding modules behind.

Exit condition: the new folders contain the real implementations and the old
flat modules can be deleted.

### 3. Extract shared builders and helper logic

- Move per-target stack construction into `builders/aws.py`, `builders/gcp.py`,
  and `builders/local.py`.
- Move shared naming and route helpers into `builders/common.py` and
  `builders/apigw.py`.
- Deduplicate `_resource_slug`, `_pulumi_env`, local image naming, container
  naming, and related helper logic so each rule exists in one place.

Exit condition: target modules no longer own low-level helper duplication.

### 4. Collapse the registry adapters

- Replace the three concrete target classes with one `PulumiDeployTarget`.
- Introduce a small frozen `TargetStrategy` dataclass in
  `targets/registry.py`.
- Add one `PulumiRunner` Protocol implemented by `pulumi/cli.py` and
  `pulumi/automation.py`.
- Keep target-specific logic in strategy callables, not in subclass trees.

Exit condition: registry size is driven by data and strategy objects instead of
copy-pasted classes.

### 5. Move target entry modules to their final role

- `targets/aws.py`, `targets/gcp.py`, and `targets/local.py` become the only
  target entry modules.
- Each target module should own just three things: artifact generation,
  strategy assembly, and target-specific constants.
- Packaging code, Pulumi orchestration, and low-level builder helpers must stay
  out of these files.

Exit condition: target modules are small and obvious to read.

### 6. Update all call sites in the same change

- Update `skaal/cli/build_cmd.py` to import from the final registry location.
- Rewrite deploy tests to import either the public facade or the new final
  internal module locations.
- Remove all references to `push.py`, `_backends.py`, `local_automation.py`,
  `aws.py`, `gcp.py`, and `local.py` once the replacements exist.

Exit condition: no code in `skaal/` or `tests/` imports deleted legacy modules.

### 7. Delete the legacy surface

- Delete `push.py`.
- Delete `local_automation.py`.
- Delete `_backends.py`, `_deps.py`, `_external.py`, and `_render.py`.
- Delete the old flat target modules once the final `targets/*` modules are in
  place.

Exit condition: `skaal.deploy` has one coherent package layout and no mixed old
and new structure.

## Unification Rules Across Submodules

The deploy refactor should align with the rest of the codebase instead of
introducing parallel abstractions.

- Use `PlanFile` and its nested specs directly; do not mirror them under
  `skaal.deploy`.
- Keep catalog-backed deploy configuration in `skaal.deploy.config`; deploy
  builders should consume those models rather than re-parse raw dicts.
- Follow the existing small-Protocol style already used in other subsystems
  when defining `AppLike` and `PulumiRunner`.
- Keep generic structural types in `skaal.types.deploy` so future runtime,
  CLI, or test code can share them without importing implementation modules.
- Treat metadata, stack outputs, and gateway route configuration as typed
  objects shared across packaging, Pulumi, and target code.

## Acceptance Criteria

The refactor is complete when all of the following are true:

- `skaal.deploy` no longer contains compatibility shims or duplicate flat
  module implementations.
- `registry.py` contains one shared adapter class, not one class per target.
- deploy-facing interfaces no longer use `stack_profile: dict[str, Any]`,
  `config_overrides: dict[str, str] | None`, or `app: Any`.
- duplicated helper functions such as resource slugging and Pulumi env setup
  exist in one location only.
- tests import from the final package layout instead of deleted legacy modules.
- the deploy-focused test suite passes after the move.

## Suggested Validation

Run the deploy and CLI test slices after the cutover, then rerun any runtime
tests that exercise plan-driven backend wiring.

Minimum expected validation:

- `pytest tests/deploy -q`
- `pytest tests/cli -q`
- `pytest tests/runtime -q`

If the repository adds a dedicated type-check target for deploy, it should be
part of the acceptance gate for this refactor.
