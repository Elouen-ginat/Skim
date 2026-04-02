# ADR 005 — Local Runtime Design

**Status:** Accepted  
**Date:** 2024-01-01

## Context

Developers need to run and test Skaal applications locally without provisioning
real infrastructure.  The local runtime must be lightweight, zero-dependency
(no Docker, no external services by default), and match the production interface.

## Decision

`skaal.local.runtime.LocalRuntime` implements a minimal asyncio TCP HTTP server:

- Each `@app.function()` becomes a `POST /{name}` endpoint.
- All storage classes are patched with in-memory `LocalMap` backends.
- `GET /` returns a JSON index of available endpoints.
- `GET /health` returns `{"status": "ok"}`.

Optional factory methods (`from_redis`, `from_sqlite`) swap in persistent
backends for integration testing.

The same `LocalRuntime` is used by `pytest` via `patch_storage_class`, ensuring
tests run against the same dispatch logic as the local server.

## Consequences

**Positive:**
- Zero external dependencies for `skaal run` and unit tests.
- Identical interface to production (POST JSON → JSON).
- Easy to swap backends via `backend_overrides`.

**Negative:**
- Not production-grade (no TLS, no auth, single-process).
- TCP parsing is hand-rolled; edge cases may exist.
