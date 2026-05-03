# ADR 024 — Secret Injection at Deploy & Runtime Implementation Plan

**Status:** Proposed
**Date:** 2026-05-03
**Related:** [user_gaps.md §B.6](../user_gaps.md#b6-compute--functions), [user_gaps.md §B.9](../user_gaps.md#b9-multi-target--multi-env--secrets), [skaal/components.py](../../skaal/components.py), [skaal/deploy/backends/external.py](../../skaal/deploy/backends/external.py), [skaal/plan.py](../../skaal/plan.py)

## Goal

Make secrets a first-class declarative resource — the same shape as `@app.storage`,
`@app.function`, `@app.blob`. The user writes one line:

```python
dsn = Secret("DB_DSN", provider="aws-secrets-manager")
```

…and Skaal handles the rest: plan-file recording, deploy-time provisioning of the
managed-secret resource, IAM grants on the function role, runtime resolution
with caching, and masked logging.

This pass closes the P0 gap from `user_gaps.md` Top-of-list item #7 and the rows in
§B.6 ("Secret injection at deploy / runtime") and §B.9 ("Env-specific config / secrets").

## Why this is next

The previous Top-of-list items have all landed:

1. blob — [ADR 016](016-blob-storage-tier-implementation-plan.md)
2. agent persistence — [ADR 018](018-agent-persistence-implementation-plan.md)
3. `skaal init` / dev workflow — folded into `skaal run`
4. solver diagnostics — [ADR 021](021-solver-diagnostics-implementation-plan.md)
5. catalog overrides — [ADR 022](022-catalog-overrides-implementation-plan.md)
6. relational migrations — [ADR 023](023-relational-migrations-implementation-plan.md)

That leaves **secret injection** as the next P0. Today the framework promises
secret injection in `ExternalComponent`'s docstring (`components.py:74-77`) but
delivers nothing beyond passing through an env-var name. `external_env_vars`
emits `${env:NAME}` placeholders (`deploy/backends/external.py:24-36`); both
AWS and GCP builders dump those straight into Lambda/Cloud Run env
(`builders/aws.py:224`, `builders/gcp.py:134`). Nothing fetches from AWS Secrets
Manager / GCP Secret Manager / Pulumi secrets. No `@app.secret`, no `Secret`
type, no `SecretSpec` in the plan.

## Scope

This pass includes:

- A typed `Secret` declaration (`SecretRef`) and plan-file shape (`SecretSpec`).
- Decorator + module + app surface: `@compute(secrets=[...])`, `Module.secret(...)`,
  `App.secret(...)`.
- A runtime `SecretRegistry` with provider-strategy resolvers (env, AWS Secrets
  Manager, GCP Secret Manager, Pulumi config).
- A deploy-time `SecretInjector` per cloud target that emits the right env wiring
  and IAM grants.
- Replacement of the `ExternalComponent.connection_env: str` slot with a typed
  `secret: SecretRef | None` field (no backward-compat shim).
- Tests for: env resolution, missing-required failure, JSON-field plucking, AWS
  IAM grant emission, GCP IAM binding emission, plan-file round-trip, masked logging.

This pass does **not** include:

- Secret rotation primitives.
- Per-secret access auditing beyond the structured-log line at resolution.
- Vault / Hashicorp / Doppler / 1Password integrations — the `SecretResolver`
  protocol is open so those can land in follow-ups.
- Secret values declared in catalog TOML — secrets are app-side declarations,
  not infrastructure-catalog entries.

## Design

### Declaration

`Secret` is a frozen dataclass in `skaal/types/secret.py`. The user writes
declarations at module or function scope; the App collects them into
`PlanFile.secrets` at plan time.

```python
@dataclass(frozen=True, slots=True)
class SecretRef:
    name: str                           # logical id; runtime key
    provider: SecretProvider = "env"    # env | aws-secrets-manager | gcp-secret-manager | pulumi-config
    source: str | None = None           # provider-specific reference (ARN, path, env name…)
    env: str | None = None              # env-var name to inject into; defaults to `name`
    required: bool = True               # missing → raise at warmup if True; else None
    json_field: str | None = None       # if value is JSON, pluck this field
```

The plan-file mirror is `SecretSpec` — same fields, but `source` and `env` are
always populated (defaults resolved). Two types because the in-Python form
favours brevity and the JSON form favours determinism.

### Provider matrix

| provider | deploy-time wiring | runtime resolution |
|---|---|---|
| `env` | env var passed through verbatim from the deploy environment to the function. | `os.environ[spec.env]`. |
| `aws-secrets-manager` | Lambda env carries the **ARN**; IAM role gets `secretsmanager:GetSecretValue` scoped to that ARN. | `aioboto3` `GetSecretValue` on first access; cached. `json_field` plucks. |
| `gcp-secret-manager` | Cloud Run `env_from.secret_key_ref` injects the live value into env directly. IAM `roles/secretmanager.secretAccessor` bound to the runtime SA. | `os.environ[spec.env]` (Cloud Run already injected); local dev uses the SDK. |
| `pulumi-config` | Pulumi `Config.require_secret` materialises the value into the function env at deploy time (encrypted-at-rest in the Pulumi state). | `os.environ[spec.env]`. |

Three of four providers resolve at runtime through env, so the hot path is
free; only AWS Secrets Manager calls the SDK (because Lambda env can carry
the ARN but not the live rotated value).

### Runtime — `SecretRegistry`

One per `App`, mirrors the lifecycle hooks of `AgentRegistry`:

- `await registry.warmup()` — eagerly resolves every `required=True` secret at
  app start so missing values fail fast at boot, not first request.
- `await registry.get(name) -> str | None` — async cache; subsequent calls are
  free.
- `await registry.close()` — closes any provider-side clients.

`ResolvedSecret` carries the value plus provenance. Its `__repr__` masks the
value (`"***"` if present, `"<missing>"` if not) so structured logs and
debug dumps never accidentally print credentials.

### Deploy — `SecretInjector`

Each cloud target owns a `SecretInjector` implementation:

- `LocalSecretInjector` — emits `NAME=${env:NAME}` placeholders into the
  docker-compose env block (same shape as the current `external_env_vars`).
- `AwsSecretInjector` — emits per-secret Pulumi `aws.secretsmanager.Secret`
  references and IAM policy statements, scoped to the specific ARN.
- `GcpSecretInjector` — emits `gcp.secretmanager.SecretIamMember` bindings and
  Cloud Run `env_from.secret_key_ref` env mappings.

The injector replaces `external_env_vars` in the AWS and GCP builders. Builders
call `injector.env_vars(plan)` and `injector.iam_statements(plan)` — two methods,
one shape across providers.

### `ExternalComponent` change

`connection_env: str | None` is removed. `secret: SecretRef | None` takes its
place. `ComponentSpec.connection_env` likewise drops; the spec carries
`secret_name: str | None` referencing the entry in `PlanFile.secrets`. There is
no back-compat shim per the implementation directive — call sites in
`examples/` and `tests/` migrate in the same pass.

### Concurrency / caching

The registry's cache is an `asyncio.Lock`-guarded `dict[str, ResolvedSecret]`.
Two concurrent `get("DB_DSN")` calls only trigger one resolver call. There is
no TTL — values live for the lifetime of the process. Rotation is out of scope
for this pass; for AWS, this matches Secrets Manager's typical "rotate +
restart" usage pattern.

## Files touched

| File | Change |
|---|---|
| `skaal/types/secret.py` | **New** — `SecretRef`, `SecretSpec`, `SecretProvider`, `ResolvedSecret`, `SecretResolver`, `SecretGrant`, `SecretMissingError`. |
| `skaal/types/__init__.py` | Export new symbols. |
| `skaal/secrets/__init__.py` | **New** — `SecretRegistry`, `EnvResolver`, dispatch table, `Secret` re-export. |
| `skaal/secrets/aws.py` | **New** (lazy) — `AwsSecretsManagerResolver`. |
| `skaal/secrets/gcp.py` | **New** (lazy) — `GcpSecretManagerResolver`. |
| `skaal/secrets/pulumi.py` | **New** — `PulumiConfigResolver` (env-passthrough at runtime). |
| `skaal/__init__.py` | Export `Secret`, `SecretRegistry`. |
| `skaal/decorators.py` | `compute(..., secrets=[...])` collects `__skaal_secrets__` on the wrapped callable. |
| `skaal/module.py` | `Module.secret(spec)`; `Module._collect_secrets()` → `dict[str, SecretRef]`. |
| `skaal/app.py` | `App.secrets: SecretRegistry`; `App.secret(spec)` shortcut. |
| `skaal/plan.py` | New `SecretSpec` field + `PlanFile.secrets: dict[str, SecretSpec]`. |
| `skaal/solver/solver.py` | Pass-through: walk declared secrets, emit `SecretSpec` into the plan. |
| `skaal/components.py` | `ExternalComponent` accepts `secret: SecretRef \| None` instead of `connection_env: str`. **Breaking** — no shim. |
| `skaal/deploy/backends/external.py` | Delete `external_env_vars` (replaced by `SecretInjector`). |
| `skaal/deploy/secrets.py` | **New** — `SecretInjector` protocol; `LocalSecretInjector`, `AwsSecretInjector`, `GcpSecretInjector`. |
| `skaal/deploy/builders/aws.py` | Consume `AwsSecretInjector(plan)`; attach IAM statements to the Lambda role. |
| `skaal/deploy/builders/gcp.py` | Consume `GcpSecretInjector(plan)`; attach `SecretIamMember`. |
| `skaal/deploy/builders/local.py` | Consume `LocalSecretInjector(plan)`. |
| `skaal/runtime/local.py` | At boot, build `SecretRegistry` from `PlanFile.secrets`; `await registry.warmup()`. |
| `skaal/runtime/middleware.py` | Inject `ctx.secrets` proxy; cached per request. |
| `skaal/cli/plan_cmd.py` | Print a "Secrets" table — name, provider, env var. Values masked. |
| `pyproject.toml` | `secrets-aws = ["aioboto3"]`, `secrets-gcp = ["google-cloud-secret-manager"]` extras. |
| `tests/secrets/test_registry.py` | Env resolver, JSON plucking, masking, missing-required, missing-optional. |
| `tests/secrets/test_aws_resolver.py` | Mocked SDK, caching, IAM grant emission. |
| `tests/secrets/test_gcp_resolver.py` | Mocked SDK, version pinning. |
| `tests/deploy/test_secret_injection.py` | AWS env+IAM, GCP env+binding, local docker-compose. |
| `tests/runtime/test_secret_warmup.py` | Required missing fails fast; optional missing returns None. |
| `docs/secrets.md` | Short usage page. |
| `examples/02_todo_api/app.py` | Use `Secret(...)` to demonstrate the API. |

## Tests

1. `Secret("DB", provider="env")` reads `os.environ["DB"]`.
2. `Secret("DB", provider="env", env="OTHER")` reads `os.environ["OTHER"]`.
3. `Secret("DB", required=True)` missing → `SecretMissingError` at warmup.
4. `Secret("DB", required=False)` missing → `registry.get("DB")` returns `None`.
5. JSON-field plucking — `Secret("DB", provider="aws-secrets-manager", json_field="dsn")`.
6. Caching — two `await get("DB")` calls trigger one SDK call.
7. AWS builder — Lambda env maps `DB` → ARN; IAM role policy contains
   `secretsmanager:GetSecretValue` scoped to that ARN only (least-privilege).
8. GCP builder — `SecretIamMember` for the Cloud Run service account; env-var
   `value_from.secret_key_ref` set.
9. Local builder — docker-compose env block contains `DB=${env:DB}`.
10. Plan round-trip — `SecretRef → SecretSpec → JSON → SecretSpec` resolves to
    the same value.
11. Masking — `repr(ResolvedSecret(value="hunter2"))` does not contain `hunter2`.

## Migration / compatibility

Per the implementation directive, no backward-compatibility shims:

- `ExternalComponent(connection_env="X")` → `ExternalComponent(secret=Secret("X"))`.
- `ComponentSpec.connection_env` → `ComponentSpec.secret_name`.
- `external_env_vars` is deleted.
- All internal call sites and examples are migrated in the same pass.

External users running the alpha will need to update declarations once. The
breaking change is intentional — the old slot was strictly less expressive
and could not carry IAM/JSON/required information.

## Open questions

- Whether the `Secret` declaration should also be allowed inside a `Module` so
  reusable modules can declare their own secrets (probably yes — same pattern
  as `Module.attach`).
- Whether AWS Secrets Manager's *runtime* SDK call should be made through the
  Lambda Extensions cache layer instead of `aioboto3`. Faster cold starts,
  but adds an extension dependency. Deferred until a benchmark proves it
  matters.
- Vault and 1Password resolvers — flagged as natural follow-ups behind the
  same `SecretResolver` protocol. No work in this pass.
