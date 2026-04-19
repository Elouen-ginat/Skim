The project itself declares Development Status :: 3 - Alpha in pyproject.toml:14 at v0.2.0, and the code matches that.

Notable gaps I see:

Maturity / stability

Alpha classifier, 0.2.x — no API-stability guarantees. Mesh runtime only landed in "Phase 3" (fc817fa) and is young.
GPL-3.0 license is a hard blocker for many commercial users.
Cloud backends are thin KV shims

PostgresBackend stores everything in one skaal_kv(ns, key, value JSONB) table — that's a KV facade, not a relational backend. Same shape for DynamoDB and Firestore. Real prod workloads on SQL would want per-entity schemas + indexes.
No connection retry / circuit breaker / pool tuning beyond min_size/max_size.
Observability & ops

Almost no logging in skaal/ (5 hits, mostly incidental). No metrics, tracing, or structured logs. You cannot operate this blind in prod.
No health-check/readiness endpoints exposed by the runtime.
Security

The HTTP runtime in skaal/runtime/local.py exposes @app.function() as POST endpoints with no authentication, rate limiting, or request size limits.
No secrets-management story for catalog/deploy config.
Robustness

Several silent except Exception # noqa: BLE001 paths in api.py (_coerce_plan, _collect_migrations, _build_config_overrides) that swallow real errors.
All cloud tests are mocked — no soak/integration tests against real AWS/GCP in tests/.
Docs

ADRs exist but no ops runbook, no upgrade/compat policy, no production deployment guide.
Solid skeleton — solver, migration engine, plan/lock model, CI, mesh — but I'd call it a strong alpha, probably 6–12 months of hardening from "ship a prod workload on it."
