# ADR 017 — Production Runtime Baseline Implementation Plan

**Status:** Proposed
**Date:** 2026-04-30
**Related:** [what_is_needed_for_prod.md](../what_is_needed_for_prod.md), [ADR 014](014-http-routing-overhaul.md), [ADR 013](013-logging-and-cli-verbosity.md)

## Goal

Land the smallest coherent implementation pass that answers the highest-impact **production** feedback still open in Skaal.

This pass should close these two gaps together:

1. authenticated `@app.function` HTTP ingress
2. a real production probe and telemetry surface: `/health`, `/ready`, and OpenTelemetry export

This is intentionally **not** a general "prod hardening" milestone. It is the minimum runtime/deploy cut that makes Skaal less "internal-use-only by accident" and gives operators a scrape/probe surface.

## Why this is next

The current production-gap list still contains many legitimate concerns, but most of them are either:

- broad maturity questions (license, alpha stability, mesh age)
- documentation/process deliverables (runbook, upgrade guide, changelog)
- separate infrastructure projects (secrets, backend retry, real-cloud soak testing)

The smallest code-only pass with the biggest production payoff is narrower:

- require auth when Skaal itself is serving function ingress
- expose a readiness contract distinct from liveness
- emit typed OpenTelemetry signals for the request path and runtime engines, exportable to SigNoz

Anything broader turns one shippable runtime hardening pass into multiple unrelated platform projects.

## Scope

This pass includes:

- JWT auth enforcement for Skaal-owned function ingress
- consistent consumption of `APIGateway.auth` for Skaal-owned HTTP paths
- `/ready` in local runtime and mounted ASGI/WSGI shims
- OpenTelemetry instrumentation for request, auth, and runtime-engine activity
- OTLP export configured for SigNoz ingestion
- typed config and context surfaces in `skaal.types` for auth and telemetry
- tests for auth, readiness, telemetry, and deploy wiring

This pass does **not** include:

- runbooks, deployment guides, upgrade docs, changelog work, or any other documentation-only feedback
- license or version-policy work
- secrets management
- backend connection retry / circuit breakers / pool tuning
- a dedicated Prometheus `/metrics` exposition endpoint
- StatsD or parallel observability pipelines
- CORS redesign or request-schema validation
- non-JWT auth providers (`oauth2`, `api-key`, `mtls`)
- real-cloud soak or chaos testing

The point is to respond to the runtime/deploy feedback with the minimum high-value code change, not to absorb every prod checklist item.

## Current facts

Today, the relevant implementation state is mixed:

- `APIGateway.auth` already exists as a real component surface in `skaal/components.py` with `provider`, `issuer`, `audience`, `header`, and `required`.
- `skaal.types.deploy.AuthConfig` is currently narrower than the component-level auth surface and needs to catch up so deploy/runtime code can stop falling back to ad-hoc `dict[str, Any]` handling.
- AWS API Gateway and GCP API Gateway builders already have **partial** JWT wiring in `skaal/deploy/builders/apigw.py`.
- Local Kong config generation also has a partial JWT plugin path in `skaal/deploy/builders/local.py`.
- `LocalRuntime` still accepts unauthenticated `POST /_skaal/invoke/<qualified_name>` requests.
- `GET /health` exists, but `/ready` does not.
- There is no OpenTelemetry instrumentation or OTLP exporter wiring for Skaal-managed ingress/runtime events.
- `InvokeContext` carries function metadata only; it has no auth claims, request metadata, or trace context.

That means this is not a greenfield auth project. The real gap is consistency at the runtime boundary.

## Decision

Make `APIGateway.auth` the single supported source of Skaal-managed ingress auth policy for this pass, and support **JWT only**.

Do not build a general auth abstraction in v1 of this cut.

Also choose **OpenTelemetry via OTLP** as the only observability pipeline for this pass, with **SigNoz** as the reference backend.

Do not add a second exporter path in the same milestone.

This keeps the implementation small and production-useful:

- JWT is already the partially wired provider across deploy targets.
- SigNoz consumes standard OTLP signals, so Skaal can integrate once without inventing a Signoz-specific API.
- typed telemetry config fits cleanly into the existing `skaal.types` deploy/runtime surfaces.

## Typing-first constraint

This implementation should treat typing as part of the design, not cleanup after the feature lands.

Specifically:

- no new long-lived observability or auth surfaces should be introduced as raw `dict[str, Any]`
- new runtime/deploy config should be represented in `skaal.types` first, then consumed by runtime and builders
- invocation metadata added for auth or tracing should have a typed shape in `skaal.types.invoke` or a new `skaal.types.observability` module

The target is to make auth and telemetry flows statically understandable before any runtime wiring is added.

## Behavior to add

### 1. JWT auth on Skaal-owned ingress

When an app attaches an `APIGateway` with:

```python
auth=AuthConfig(provider="jwt", issuer="...", audience="...")
```

the Skaal runtime should enforce JWT validation on Skaal-owned invocation routes.

For this pass, the protected surface is:

- `POST /_skaal/invoke/<qualified_name>`
- mounted-runtime passthroughs that Skaal itself owns around the invoke seam

It should **not** attempt to become the auth system for the user’s mounted FastAPI/Starlette/Flask application. Mounted frameworks still own their own public router behavior.

### 2. Auth semantics

Support only `provider="jwt"`.

Validation rules:

- `issuer` is required
- `header` defaults to `Authorization`
- bearer-token parsing is required when `header == "Authorization"`
- `required=false` means missing token is allowed, but present tokens must still validate
- conflicting `APIGateway.auth` configs across attached gateways should fail clearly during plan/build

Use JWKS-backed token verification with a lightweight library rather than bespoke crypto.

### 3. Auth context propagation

Extend `InvokeContext` with optional auth metadata so hooks and handlers can inspect verified identity without a new decorator API.

Minimal additions:

- request headers (read-only)
- `auth_claims: dict[str, Any] | None`
- `auth_subject: str | None`
- `trace_id: str | None`
- `span_id: str | None`

Do not add framework-specific request objects to handler signatures.

These additions should be expressed in typed form rather than left as protocol comments only.

### 4. `/ready`

Keep `/health` as a shallow liveness check.

Add `/ready` as a readiness check that returns 200 only when:

- storage/runtime wiring is complete
- engine startup has completed
- any required auth verifier initialization has succeeded
- required backend clients are reachable enough for the runtime to serve traffic

Return 503 when startup is incomplete or when a required dependency is unavailable.

### 5. OpenTelemetry export

Instrument the runtime with OpenTelemetry and export over OTLP to SigNoz.

The initial signal set should be intentionally small:

- HTTP server spans for Skaal-owned invoke paths
- request counters, latency histograms, and in-flight request metrics
- auth success/failure counters
- runtime readiness and engine lifecycle metrics
- engine backlog gauges only when they are cheap and meaningful

If an engine cannot produce a meaningful backlog metric cheaply, it should omit that metric rather than fake precision.

The plan should not depend on a Prometheus text endpoint as the primary production contract.

## Runtime design

### Runtime auth middleware

Add one runtime-owned auth layer in front of Skaal’s invoke seam rather than scattering checks across handlers.

That layer should:

- read the effective `APIGateway.auth` config from the app
- validate the JWT before dispatch
- attach claims to the invocation context
- attach trace metadata so spans and handler hooks can correlate identity to work
- reject with 401 or 403 before entering the resilience wrapper

This should live alongside the existing request parsing and middleware path, not inside individual `@app.function` bodies.

### Runtime readiness state

Add an explicit readiness state machine to the runtime:

- `starting`
- `ready`
- `degraded`
- `stopped`

This should be derived from real runtime state, not a static flag.

### Runtime telemetry pipeline

Add one small runtime-owned OpenTelemetry integration layer.

Do not pull in a second observability abstraction on top of OpenTelemetry.

Required responsibilities:

- create spans around Skaal-managed invoke dispatch and engine work boundaries
- record request, auth, readiness, and engine metrics via the OTel SDK
- export OTLP to a collector / SigNoz endpoint when configured
- degrade to effectively no-op instrumentation when telemetry is disabled

### Typed config surfaces

Before wiring the runtime, add or tighten the relevant types in `skaal.types`.

Minimum expected changes:

- extend `skaal.types.deploy.AuthConfig` with `header` and `required`
- add a new typed telemetry config surface, preferably in `skaal/types/observability.py`, re-exported from `skaal/types/__init__.py`
- add typed invocation auth/trace metadata in `skaal.types.invoke`
- add a typed readiness-state representation instead of stringly-typed status plumbing

The telemetry config should stay small and generic. A reasonable first cut is:

```python
TelemetryExporter = Literal["otlp"]

class TelemetryConfig(TypedDict, total=False):
	exporter: TelemetryExporter
	endpoint: str
	service_name: str
	service_namespace: str
	headers: dict[str, str]
	insecure: bool
```

SigNoz-specific behavior should stay in documentation and defaults, not in the public type name.

## Engine telemetry contract

Introduce a tiny optional engine metrics hook, for example:

```python
class EngineTelemetrySnapshot(TypedDict, total=False):
	running: bool
	failures: int
	queue_depth: int
	active_tasks: int


def snapshot_telemetry(self) -> EngineTelemetrySnapshot: ...
```

The runtime can poll this hook while producing OTel metric observations.

This is enough for the first pass. Do not build a streaming metrics bus.

Likely initial implementations:

- `ProjectionEngine`: running state, failure count, lag/offset gap when available
- `OutboxEngine`: running state, pending relay queue depth when available
- `SagaEngine`: running state, active saga count
- `EventLogEngine`: running state only if no backlog concept exists

## Deploy implications

### AWS

Keep using API Gateway HTTP API JWT authorizers, but make the runtime behavior match the gateway contract.

This pass should ensure:

- routes protected by `APIGateway.auth` map consistently to the runtime invoke seam
- `/health` and `/ready` remain reachable for probes
- OTLP exporter settings can be supplied to the runtime without ad-hoc env-var conventions leaking across targets

Do not add a separate custom Lambda authorizer path in this pass.

### GCP

Keep using API Gateway OpenAPI JWT config.

This pass should ensure the generated OpenAPI and runtime behavior match each other rather than leaving auth as a partially declarative no-op, and that telemetry env/config injection matches the typed deploy surface.

### Local / Docker / Kong

Keep the local deploy path minimal:

- preserve the existing Kong JWT plugin direction
- make direct `LocalRuntime` serving respect the same JWT config on Skaal-owned invoke paths
- allow a local OTLP collector or SigNoz endpoint to be configured through the same typed telemetry shape

This matters because local should exercise the same auth contract developers will deploy.

## Public API changes

This pass should avoid adding broad new configuration surfaces.

The only public-surface changes should be:

- `APIGateway.auth` becomes operational for `provider="jwt"`
- `/ready` becomes a reserved runtime endpoint next to `/health`
- `InvokeContext` gains optional auth and trace metadata
- a typed telemetry config surface is added to `skaal.types`

Do not add a new `@auth_required` decorator or a second gateway-auth config surface.

## Validation and failure behavior

This pass should also tighten runtime behavior around these new paths:

- auth failures return clear 401/403 responses without tracebacks
- readiness failures return a compact dependency/status payload
- telemetry export failures should not crash the runtime; they should be logged and reflected in telemetry-health state

If a required JWT verifier cannot initialize, readiness must stay false.

## Test plan

Required coverage:

- local runtime test: unauthenticated invoke rejected when JWT auth is configured
- local runtime test: valid token reaches handler and claims are available in invoke context
- readiness test: `/health` stays 200 while `/ready` reflects backend/engine state
- telemetry test: spans and metrics are emitted through an in-memory OTLP/OpenTelemetry test exporter after real invocations
- deploy tests: AWS/GCP/local artifact generation keeps auth config and typed telemetry config injection intact
- negative test: conflicting gateway auth configs fail clearly
- typing test: new auth and telemetry surfaces round-trip through `skaal.types` without `Any` fallbacks

Do not require live IdP infrastructure in the default test suite. Use static JWKS fixtures.

## Rollout order

Implement in this sequence:

1. define the effective auth-policy resolution rules from attached `APIGateway` components
2. tighten the relevant `skaal.types` surfaces before runtime wiring (`AuthConfig`, invocation metadata, telemetry config)
3. add runtime JWT verification on the Skaal invoke seam
4. extend `InvokeContext` with auth and trace metadata
5. add readiness state and `/ready`
6. add the OpenTelemetry integration layer and OTLP export path
7. add engine telemetry hooks where backlog is cheap to compute
8. align deploy builders and tests across AWS, GCP, and local

This keeps the first executable validation target small: auth works locally, `/ready` flips correctly, and OTel signals are observable in tests.

## Non-goals and follow-ups

Leave these for later passes:

- StatsD export
- Prometheus text exposition as a separate export surface
- secrets-manager integration
- backend retry policies
- full CORS and schema-validation hardening
- production runbooks and upgrade/process documents
- commercial-license or API-stability policy work

The right next production milestone is not "solve prod forever." It is:

authenticated Skaal-managed ingress, a real readiness contract, and typed OpenTelemetry signals flowing into SigNoz.
