# What's needed for prod

Operational, security, and maturity gaps tracked toward "ship a prod workload on it."
For ergonomic and capability gaps from a user's perspective, see [`user_gaps.md`](./user_gaps.md).

Status as of 2026-04: still alpha. Several items from the original audit have moved.

Legend:
- ✅ done
- 🟡 partial — primary gap closed, secondary work remains
- ❌ open

---

## Maturity / stability

- ❌ **Alpha classifier, version 0.2.0**. `pyproject.toml:14` still declares `Development Status :: 3 - Alpha`; `pyproject.toml:7` still pins `0.2.0`. No public API-stability or deprecation policy.
- ❌ **GPL-3.0 license**. Unchanged in `LICENSE` and `pyproject.toml:16`. Hard blocker for many commercial users.
- ❌ **Mesh runtime is young**. Phase-3-vintage code in `skaal/mesh/` and `skaal/runtime/mesh_runtime.py`.

## Cloud backends

- 🟡 **Postgres KV-only facade** — partly addressed. The relational tier landed: `skaal/relational.py` plus `PostgresBackend._ensure_relational_engine`, `ensure_relational_schema`, `open_relational_session` (`skaal/backends/postgres_backend.py:95-107, 242-255`). `@app.relational` resolves SQLModel classes to real per-entity tables. The `skaal_kv(ns, key, value JSONB)` table (`postgres_backend.py:18-23`) is still the path for `@app.storage` (KV tier), which is the design.
- ❌ **DynamoDB and Firestore are still KV-only**. No relational analogue — `dynamodb_backend.py`, `firestore_backend.py`. Production SQL workloads on those clouds still have no per-entity schema.
- ❌ **No backend connection retry / circuit breaker / pool tuning beyond `min_size/max_size`.** Application-level resilience landed for `@app.function` (`skaal/runtime/middleware.py`, wired in `runtime/local.py:66-71`), but **backend connections** have no retry. A transient asyncpg/boto3/firestore network hiccup is fatal in `Backend.get/set`. `DynamoDBBackend.atomic_update` (`backends/dynamodb_backend.py:219-229`) retries only on version conflicts, not on network errors.

## Observability and ops

- 🟡 **Logging**. Substantially improved since the original "5 hits" audit. `skaal/_logging.py` and `skaal/cli/_logging.py` provide a real logger setup with `TextLogFormatter` and `JsonLogFormatter`; ~120 log calls now exist across the package. Backends remain mostly silent on errors — that's the residual gap.
- ❌ **No telemetry pipeline.** No OpenTelemetry instrumentation, no OTLP export to SigNoz, and no tracing/metrics integration. `skaal/components.py:20` *declares* a "prometheus" component type but the runtime emits nothing.
- 🟡 **Health endpoints**. `GET /health` exists (`runtime/local.py:353, 491, 598, 796, 852`) and returns `{"status": "ok", "app": <name>}`. `/ready` (liveness vs. readiness) is still missing, and there is still no OpenTelemetry export path for runtime/request telemetry.
- ❌ **No ops runbook, no upgrade/compat policy, no production deploy guide.** `docs/design/` has 13 ADRs; there is no `RUNBOOK.md`, `UPGRADE.md`, `CHANGELOG.md`, or `DEPLOYMENT.md`.

## Security

- 🟡 **HTTP runtime**. Request body size is now capped at 10 MiB (`runtime/local.py:14`, enforced at 429-440 → 413 Payload Too Large). Per-function rate limiting is wired through `runtime/middleware.py`. **No authentication anywhere** — `APIGateway.auth` (`components.py:191-230`) is declared but no deploy target consumes it; `local.py:340-397` exposes every `@app.function` as an unauthenticated POST. **No CORS** middleware. **No request schema validation** beyond "body must be JSON object."
- ❌ **No secrets management.** `ExternalComponent.connection_env` (`components.py:79-100`) is a slot that names an env var; nothing fetches from AWS Secrets Manager / GCP Secret Manager / Pulumi secrets. Users hand-wire secret injection via cloud-native means.

## Robustness

- ❌ **Silent `except Exception  # noqa: BLE001` paths.** Still present in `skaal/api.py:589, 818, 870, 964`, also `runtime/local.py:394, 455`, and `runtime/engines/projection.py:52-56` (the projection-handler swallow is documented as "strict-mode in a later phase"). At minimum these should log; ideally route to a DLQ or strict-mode hook.
- ❌ **No real-cloud integration tests.** `tests/backends/test_backend_contract.py` parametrizes over backends and gracefully skips when the service isn't reachable — useful for local Postgres/Redis, but no soak/load tests against real AWS/GCP, no chaos.

## What's next

The two highest-impact items still on this list:

Tracked together in [ADR 017](./design/017-production-runtime-baseline-implementation-plan.md).

1. **Auth on `@app.function` endpoints** + a wired `APIGateway.auth` consumer in deploy targets. Today the runtime is "internal use only" by accident.
2. **`/ready` + typed OpenTelemetry export** to SigNoz for the request path and engine lifecycle, with the config surfaces defined in `skaal.types` instead of `Any`-shaped dict plumbing.

Everything else is a strong-alpha project's normal hardening tail: backend retry, secrets injection, real-cloud test, runbooks, license decision.

---

For user-visible **ergonomic and capability** gaps (path-param routing, pagination, agent state durability, blob storage, etc.) see [`user_gaps.md`](./user_gaps.md).
