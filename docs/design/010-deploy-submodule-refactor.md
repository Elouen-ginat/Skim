# ADR 010 — Deploy Submodule Refactor

**Status:** Proposed
**Date:** 2026-04-29

**Execution Note:** [ADR 011](011-deploy-submodule-implementation-plan.md) supersedes the migration details in this note. The implementation should land as a direct cutover without compatibility shims.

## Context

The `skaal.deploy` package has grown to ~4,000 lines distributed across 13
flat modules. Each new target or backend pays an integration cost in three
files at once: the per-target generator, the registry adapter, and `push.py`'s
private helpers. The current shape blurs four distinct concerns:

| Concern              | Current home                                     |
| -------------------- | ------------------------------------------------ |
| Pulumi stack shape   | `aws.py`, `gcp.py`, `local.py` (each ~600 LOC)   |
| Artifact packaging   | `push.py` (`_package_aws`, `_build_push_image`)  |
| Pulumi orchestration | `push.py` (CLI) + `local_automation.py` (API)    |
| Backend wiring       | `_backends.py`, `_external.py`, `_deps.py`       |
| Target registry      | `registry.py` (3 adapters, ~80 LOC each)         |

Five empty subdirectories — `backends/`, `builders/`, `packaging/`, `pulumi/`,
`targets/` — already exist as placeholders for this split. The refactor
populates them.

Secondary problems:

- **Untyped boundaries.** `app: Any`, `stack_profile: dict[str, Any]`,
  `config_overrides: dict[str, str] | None`, `meta: dict[str, Any]`, and the
  return values of `build_wiring()` and `get_handler()` cross subsystem
  boundaries with no schema. Most are TypedDict-shaped in practice.
- **Adapter duplication.** `AWSLambdaTarget`, `GCPCloudRunTarget`, and
  `LocalDockerTarget` (`registry.py:36-271`) each reimplement the same
  three-phase pipeline (stack init → packaging → up → output) with one or two
  per-target steps interleaved.
- **Mixed Pulumi paths.** Cloud targets shell out to `pulumi` CLI; local
  uses the Automation API. The two share zero code despite needing the same
  filestate-backend handling and `PULUMI_CONFIG_PASSPHRASE` setup.
- **Helper drift.** `_resource_slug` is defined twice
  ([aws.py:34](../../skaal/deploy/aws.py#L34),
  [local.py:150](../../skaal/deploy/local.py#L150),
  [local_automation.py:24](../../skaal/deploy/local_automation.py#L24)).
  `_pulumi_env` is defined twice (`push.py:99`, `local_automation.py:52`).
  `_local_image_name` exists in both `local.py` and `local_automation.py`.

No backward compatibility is required — `deploy` is internal to the package
and the two public entry points (`package_and_push`, `get_target`) are the
only names re-exported from `skaal.deploy.__init__`.

## Decision

Reorganise `skaal.deploy` into five sub-packages aligned with the four
concerns above plus a thin top-level facade. Tighten every cross-module
boundary with a TypedDict or Protocol. Collapse the three adapter classes
into a single `PulumiDeployTarget` driven by per-target strategy callables.

## Target Layout

```
skaal/deploy/
├── __init__.py              # public API: package_and_push, destroy_stack, get_target
├── target.py                # DeployTarget Protocol
├── config.py                # Pydantic compute/storage deploy configs (unchanged location)
│
├── backends/                # everything about backend wiring
│   ├── __init__.py
│   ├── handler.py           # BackendHandler + get_handler  (was _backends.py top half)
│   ├── services.py          # _LOCAL_SERVICE_SPECS + _LOCAL_FALLBACK + _FALLBACK_WIRE
│   ├── wiring.py            # build_wiring / build_wiring_aws / _make_constructor
│   ├── external.py          # was _external.py
│   └── deps.py              # was _deps.py
│
├── builders/                # pure functions: PlanFile + App → PulumiStack dict
│   ├── __init__.py
│   ├── common.py            # _resource_slug, _safe_key, _database_name, route helpers
│   ├── apigw.py             # _add_apigw_resources (AWS) + _add_gcp_api_gateway
│   ├── aws.py               # _build_pulumi_stack_aws
│   ├── gcp.py               # _build_pulumi_stack_gcp
│   └── local.py             # _build_pulumi_stack_local
│
├── packaging/               # build deployable bundles from artifacts dirs
│   ├── __init__.py
│   ├── aws.py               # package_lambda  (was _package_aws)
│   ├── gcp.py               # build_and_push_image
│   └── local.py             # build_local_image
│
├── pulumi/                  # unified Pulumi runner
│   ├── __init__.py
│   ├── env.py               # _pulumi_env, _local_backend_url
│   ├── cli.py               # CLI subprocess helpers (was push.py _pulumi_*)
│   ├── automation.py        # Automation API (was local_automation.py body)
│   ├── meta.py              # write_meta, read_meta, MetaFile TypedDict
│   └── render.py            # was _render.py
│
├── targets/                 # one module per target, each ~150 LOC
│   ├── __init__.py
│   ├── aws.py               # generate_artifacts_aws + AWS strategy
│   ├── gcp.py               # generate_artifacts_gcp + GCP strategy
│   ├── local.py             # generate_artifacts_local + local strategy
│   └── registry.py          # PulumiDeployTarget + _TARGET_REGISTRY + get_target
│
└── templates/               # unchanged
```

`push.py` is removed; its public functions move to
`skaal/deploy/__init__.py` and import from the sub-packages.

## Type Unification

All types currently typed as `dict[str, Any]` at module boundaries become
`TypedDict`s in `skaal/types/deploy.py`. New entries:

```python
# skaal/types/deploy.py — additions

class StackProfile(TypedDict, total=False):
    env: dict[str, str]
    invokers: list[str]
    labels: dict[str, str]
    enable_mesh: bool

class DeployMeta(TypedDict, total=False):
    target: TargetName              # Literal alias, see below
    source_module: str
    app_name: str
    lambda_architecture: str
    lambda_runtime: str

class RouteSpec(TypedDict, total=False):
    path: str
    target: str
    methods: list[str]

class AuthConfig(TypedDict, total=False):
    provider: Literal["jwt"]
    issuer: str
    audience: str

class RateLimitConfig(TypedDict, total=False):
    requests_per_second: float
    burst: int

class GatewayConfig(TypedDict, total=False):
    routes: list[RouteSpec]
    auth: AuthConfig
    rate_limit: RateLimitConfig
    cors_origins: list[str]

TargetName = Literal[
    "aws", "gcp", "local",
    "aws-lambda", "gcp-cloudrun", "local-docker",
]

ConfigOverrides: TypeAlias = dict[str, str]

class BackendWiring(NamedTuple):
    imports: str
    overrides: str
```

The existing `PulumiStack`, `PulumiResource`, `LocalServiceSpec`,
`Docker*Properties` already live in `types/deploy.py` and stay put; these new
entries join them and are re-exported from `skaal.types`.

`DeployTarget` Protocol (`target.py`) tightens its signatures:

```python
def generate_artifacts(
    self,
    app: AppLike,                          # was Any → narrow Protocol w/ .name, ._mounts, ._wsgi_attribute
    plan: PlanFile,
    output_dir: Path,
    source_module: str,
    app_var: str = "app",
    *,
    region: str | None = None,
    dev: bool = False,
    stack_profile: StackProfile | None = None,
) -> list[Path]: ...

def package_and_push(
    self,
    artifacts_dir: Path,
    *,
    stack: str,
    region: str | None,
    gcp_project: str | None,
    yes: bool,
    project_root: Path,
    source_module: str,
    app_name: str,
    config_overrides: ConfigOverrides | None = None,
) -> StackOutputs: ...                     # StackOutputs = dict[str, str], named for clarity
```

`AppLike` is a small `Protocol` declared in `skaal/types/deploy.py`
exposing only the attributes the deploy code actually reads (`name`,
`_mounts`, `_wsgi_attribute`). It avoids importing `skaal.app` at deploy
load time.

## Adapter Unification

The three target adapter classes collapse into a single
`PulumiDeployTarget` parameterised by a `TargetStrategy` dataclass:

```python
# skaal/deploy/targets/registry.py

@dataclass(frozen=True)
class TargetStrategy:
    name: TargetName
    default_region: str
    generate: Callable[..., list[Path]]                # targets/<x>.py
    package: Callable[[Path, ...], None] | None        # packaging/<x>.py
    pulumi_runner: PulumiRunner                        # cli or automation
    pre_up_config: Callable[[ArtifactsCtx], dict[str, str]]   # region, project keys
    post_up_steps: Callable[[ArtifactsCtx], None] | None      # GCP: build+push image, second up
    output_keys: tuple[str, ...]                       # ("apiUrl",) etc.

class PulumiDeployTarget:
    def __init__(self, strategy: TargetStrategy): self.strategy = strategy
    # generate_artifacts / package_and_push / destroy_stack delegate to strategy
```

`PulumiRunner` is a Protocol implemented by both `pulumi/cli.py` (the
subprocess flavour used by AWS/GCP) and `pulumi/automation.py` (used by
local). Both expose `stack_select_or_init`, `config_set`, `up`, `destroy`,
`output`. This is the single biggest deduplication win — `local_automation`
stops duplicating env/backend setup.

The registry then becomes a flat dict of strategies:

```python
_TARGET_REGISTRY: dict[str, PulumiDeployTarget] = {
    "aws":           PulumiDeployTarget(_AWS_STRATEGY),
    "aws-lambda":    PulumiDeployTarget(_AWS_STRATEGY),
    "gcp":           PulumiDeployTarget(_GCP_STRATEGY),
    "gcp-cloudrun":  PulumiDeployTarget(_GCP_STRATEGY),
    "local":         PulumiDeployTarget(_LOCAL_STRATEGY),
    "local-docker":  PulumiDeployTarget(_LOCAL_STRATEGY),
}
```

Three ~30-line strategies replace three ~80-line adapter classes.

## Migration Plan

The refactor lands as a single PR (no consumer code outside `skaal/cli/` and
`tests/deploy/` references these internals). Order of work:

1. **Types first.** Add the new TypedDicts / `AppLike` / `BackendWiring` /
   `TargetName` to `skaal/types/deploy.py` and re-export them from
   `skaal/types/__init__.py`. No call-site changes yet.

2. **Move helpers into sub-packages**, leaving the existing modules as
   re-export shims temporarily so each step's diff stays reviewable:
   - `_render.py` → `pulumi/render.py`
   - `_external.py` → `backends/external.py`
   - `_deps.py` → `backends/deps.py`
   - `_backends.py` → split into `backends/handler.py`, `services.py`,
     `wiring.py`
   - `push.py` Pulumi helpers → `pulumi/cli.py`
   - `push.py` `write_meta` / `read_meta` → `pulumi/meta.py`
   - `push.py` `_package_aws` → `packaging/aws.py`
   - `push.py` `_build_push_image` → `packaging/gcp.py`
   - `push.py` `_build_local_image` → `packaging/local.py`
   - `local_automation.py` → `pulumi/automation.py`

3. **Lift `_build_pulumi_stack` per target** into `builders/<target>.py` and
   move shared helpers (`_resource_slug`, `_safe_key`, `_apigw_path`,
   `_gcp_openapi_path`, `_database_name`) into `builders/common.py` and
   `builders/apigw.py`. The remaining body of `aws.py` / `gcp.py` /
   `local.py` (just `generate_artifacts`) moves to `targets/<x>.py`.

4. **Introduce `PulumiRunner` Protocol** in `pulumi/__init__.py`. Adapt
   `cli.py` and `automation.py` to satisfy it. Replace direct subprocess
   calls in `targets/*` with runner method calls.

5. **Collapse the three adapter classes** in `registry.py` into
   `PulumiDeployTarget` + three `TargetStrategy` instances, located in
   `targets/registry.py`.

6. **Delete the shim modules** (`_backends.py`, `_deps.py`, `_external.py`,
   `_render.py`, `target.py` body migration, `push.py`,
   `local_automation.py`, the per-target `aws.py` / `gcp.py` / `local.py`)
   once nothing imports them.

7. **Update `skaal/deploy/__init__.py`** to export `package_and_push`,
   `destroy_stack`, `get_target` from their new homes.

8. **Update call sites:**
   - `skaal/cli/build_cmd.py` and `skaal/cli/deploy_cmd.py` —
     re-route `from skaal.deploy.X` imports.
   - `tests/deploy/*` — same.
   - The plan / solver does not import `skaal.deploy`, so no change there.

## Outcome

| Metric                                  | Before                | After (target)        |
| --------------------------------------- | --------------------- | --------------------- |
| Top-level files in `skaal/deploy/`      | 13                    | 3 (`__init__`, `target`, `config`) |
| Largest single module                   | `aws.py` (714 LOC)    | `builders/aws.py` (~450 LOC) |
| Untyped `dict[str, Any]` parameters     | 7                     | 0                     |
| Duplicated `_resource_slug` definitions | 3                     | 1                     |
| Adapter classes per target              | 1                     | 0 (one shared class)  |
| Pulumi helper modules                   | 2 (CLI + Automation)  | 1 unified Protocol    |

Adding a new target after the refactor is: write `builders/<x>.py`,
`packaging/<x>.py` (if needed), `targets/<x>.py`, and one `TargetStrategy`
instance. Three files, no edits to a registry adapter class, no
`push.py`-style growing god-module.
