# ADR 014 — `@app.function` as Compute Primitive; HTTP Belongs to the User

**Status:** Proposed
**Date:** 2026-04-30
**Supersedes (in-place):** an earlier draft of this ADR that proposed a Skaal-native router.
**Related:** [user_gaps.md §B.1](../user_gaps.md), [ADR 005](005-local-runtime-design.md), [ADR 010](010-deploy-submodule-refactor.md)

## Context

The user-gap review flagged the HTTP surface of `@app.function` as the highest-reach P0: only `POST /<fn>` works, no path params, no methods, no streaming, no auth. The first version of this ADR proposed a Skaal-native router with `path=` / `method=` decorator args, a `RouteTable`, brace-style pattern matching, and a `StreamResponse` wrapper.

That direction was wrong. Skaal would have been reimplementing Starlette in miniature, and `app.mount_asgi(...)` already exists (`skaal/app.py:87-123`) precisely to delegate HTTP shape to a real web framework.

The category error: treating `@app.function` as a web-framework endpoint when it is actually a **compute primitive**. The HTTP exposure at `POST /<fn>` is a calling convention used by the cloud invoker (Cloud Scheduler, EventBridge) and the local dev loop — not a public web surface that competes with FastAPI's routing.

### What `@app.function` actually does

Three responsibilities are tangled in the current design:

1. **Compute placement** — `compute=` and `scale=` constraints drive solver selection of Lambda vs Cloud Run vs container vs local; the deploy targets emit infra to host it.
2. **Resilience boundary** — every invocation is wrapped through `runtime/middleware.py` (retry, circuit breaker, rate limit, bulkhead) via `_invokers` in `skaal/runtime/local.py:66-71`.
3. **HTTP exposure** — served at `POST /<fn>` so the cloud invoker and local clients can reach it.

(1) and (2) are Skaal's value-add. (3) is incidental and should not grow.

### What the user-gap §B.1 items actually were

Re-reading them under the reframe:

| §B.1 want                                | Actual owner under reframe       |
| ---------------------------------------- | -------------------------------- |
| Path params, methods, multiple methods   | FastAPI / Starlette / Litestar   |
| Auth (OAuth/JWT/sessions)                | The user's web framework         |
| CORS, request logging, compression       | Web-framework middleware         |
| Request schema validation                | Pydantic via FastAPI             |
| OpenAPI generation                       | FastAPI auto-generates           |
| WebSockets                               | Starlette                        |
| Multipart upload                         | FastAPI                          |
| SSE / chunked streaming response         | FastAPI's `StreamingResponse`    |
| Static asset serving                     | Starlette `StaticFiles`          |

None of those are Skaal's job. The remaining real Skaal-side concerns are:

- A public, supported way to invoke a `@app.function` from inside a user-mounted FastAPI handler **with resilience applied**. Today calling the bare function bypasses retry/CB; the resilience-wrapped path is private (`_invokers`).
- The internal `POST /<fn>` calling convention should be reserved (renamed to a `/_skaal/...` prefix) so the user's mounted FastAPI owns the public root.
- Async generators returned from a `@app.function` should pass cleanly through the resilience middleware, so a FastAPI handler can wrap one in `StreamingResponse`.

## Decision

`@app.function` is documented and treated as a compute primitive plus resilience boundary. HTTP shape is the user's responsibility, owned via `mount_asgi(FastAPI())` (or Starlette / Litestar). Skaal stops trying to be a web framework and ships the small public seam that makes the FastAPI-mount path first-class.

### User-facing pattern (blessed path)

```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from skaal import App
from skaal.types import RetryPolicy, Scale

app = App("api")
api = FastAPI()

@app.function(retry=RetryPolicy(max_attempts=3), scale=Scale(instances="auto"))
async def predict(features: dict) -> dict:
    ...

@app.function()
async def chat_tokens(prompt: str):       # async generator
    async for tok in llm.stream(prompt):
        yield tok

@api.get("/items/{id}")
async def get_item(id: str) -> dict:
    return await app.invoke(predict, features={"id": id})       # resilience applied

@api.post("/chat")
async def chat_endpoint(prompt: str) -> StreamingResponse:
    stream = app.invoke_stream(chat_tokens, prompt=prompt)
    return StreamingResponse(stream, media_type="text/event-stream")

app.mount_asgi(api, attribute="api")
```

The user owns: routing, auth, validation, OpenAPI, websockets, streaming framing, CORS, static assets.
Skaal owns: where `predict` runs, how it scales, retry on transient failures, circuit breaker, rate limit, deploy.

### Three concrete changes

1. **Public `app.invoke(...)` and `app.invoke_stream(...)`.** Wrap the existing `_invokers` resilience pipeline in a public API. `invoke` returns the awaited result; `invoke_stream` returns an async iterator suitable for handing to a streaming response. Both apply the function's declared `retry`, `circuit_breaker`, `rate_limit`, `bulkhead`. Calling the bare function (`await predict(...)`) keeps working but **bypasses resilience** — documented as the local-call shortcut, not the production path.

2. **Reserve `/_skaal/invoke/<fn>` for the cloud-invoker convention.** Move the existing `POST /<fn>` and `POST /_skaal/scheduled/<fn>` (the schedule backdoor at `runtime/local.py:332`) under a single reserved prefix:
    - `POST /_skaal/invoke/<qualified_fn_name>` — generic invoker entry point used by EventBridge / Cloud Scheduler / queue triggers / the dev CLI.
    - The user's mounted ASGI app owns everything outside `/_skaal/`. The runtime rejects any user attempt to mount under `/_skaal/`.
    - Update Cloud Scheduler / EventBridge URLs in `skaal/deploy/targets/aws.py` and `gcp.py` accordingly. The current `POST /<fn>` is removed in this PR — alpha software, breaking changes documented in the changelog.

3. **Streaming through the middleware.** Confirm and test that `runtime/middleware.py`'s `ResilientInvoker` passes async generators through unchanged (today it awaits the result; for an async generator, "the result" is the generator object itself, so it should already work — but the contract is undocumented and untested). Add a test that a streamed function with `retry` declared retries the **first** yield on transient failure but does not retry mid-stream.

### Not in this PR

- Authentication. Owned by the user's web framework.
- Path-param routing, method dispatch, OpenAPI, websockets, multipart, CORS, static assets. Owned by the user's web framework.
- Pagination, secondary indexes, blob tier, agent persistence, per-row TTL — separate plans, see [user_gaps.md](../user_gaps.md).

## Types to add

Far smaller surface than the rejected draft. New types live in `skaal/types/invoke.py` and re-export from `skaal/types/__init__.py`.

```python
# skaal/types/invoke.py

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Awaitable, Callable, Protocol, TypeAlias, TypeVar

T = TypeVar("T")
T_co = TypeVar("T_co", covariant=True)


class InvokeContext(Protocol):
    """Read-only metadata for a single invocation, passed to BeforeInvoke hooks.

    Stable shape; new fields added are additive. Hooks should not assume the
    set is fixed.
    """
    function_name: str          # qualified name as in app._collect_all()
    kwargs: dict[str, Any]      # the invocation kwargs (mutable: hooks may rewrite)
    is_stream: bool             # True if invoked via app.invoke_stream
    attempt: int                # 1-based retry attempt (1 on first try)


BeforeInvoke: TypeAlias = Callable[[InvokeContext], Awaitable[None]]
"""Cross-cutting hook fired before every invocation, after resilience policies
are resolved but before the call is made. May mutate ``ctx.kwargs`` (e.g. inject
a tracing parent-span id, dedupe by an idempotency key) or raise to short-circuit.

Reserved for Skaal-owned cross-cutting concerns: tracing, idempotency, mesh
routing tags. Auth is **not** a use case — auth lives in the user's web
framework, before the call ever reaches Skaal.
"""


class StreamFn(Protocol[T_co]):
    """Structural protocol for a ``@app.function`` declared as an async generator.

    Used to type ``app.invoke_stream`` so callers get back ``AsyncIterator[T]``
    matching the function's yield type. Pure documentation aid; the runtime
    does not enforce the protocol.
    """
    def __call__(self, **kwargs: Any) -> AsyncIterator[T_co]: ...
```

That's the whole new public type surface. No `Route`, no `HttpMethod`, no `StreamResponse` — Starlette's `StreamingResponse` is the user's choice.

Re-exports added to `skaal/types/__init__.py`:

```python
from skaal.types.invoke import BeforeInvoke, InvokeContext, StreamFn

__all__ = [
    # ... existing entries ...
    # invoke
    "BeforeInvoke",
    "InvokeContext",
    "StreamFn",
]
```

`InvokeContext` and `BeforeInvoke` get re-exported from the top-level `skaal` package; `StreamFn` does not (it's a typing helper, not a runtime construct).

## Implementation steps

1. **Add `skaal/types/invoke.py`** with the three definitions. Re-export from `skaal/types/__init__.py`. Re-export `InvokeContext` and `BeforeInvoke` from `skaal/__init__.py`.

2. **Public `App.invoke` and `App.invoke_stream`.** New methods on `Module` (so they're inherited by `App`):
    - `invoke(fn, **kwargs)` — resolves the function's qualified name, looks up its `ResilientInvoker` (today private under `LocalRuntime._invokers`), runs the `BeforeInvoke` chain, awaits the call.
    - `invoke_stream(fn, **kwargs)` — same dispatch path but returns the async iterator. The resilience policies still apply to the **first** yield (so a transient connection error retries); mid-stream errors propagate.
    - Both raise `RuntimeError` if called before the runtime is up. Both accept either the function object or its qualified name string.
    - This requires lifting the `_invokers` cache out of `LocalRuntime` (where it's instance-scoped) into a runtime-context object accessible from the App. Concretely: store the active runtime as a weak reference on the App at `serve_async` / `run` time, and expose a small `RuntimeHandle` private API.

3. **Reserved-prefix routing.** In `skaal/runtime/local.py:340-397` rewrite `_dispatch`:
    - Move `POST /<fn>` and `POST /_skaal/scheduled/<fn>` (current line 332 logic) to `POST /_skaal/invoke/<qualified_name>`. Keep `GET /` and `GET /health`.
    - The user's mounted ASGI app receives anything outside `/_skaal/`.
    - At `mount_asgi` / `mount_wsgi` time, reject `prefix="/_skaal/..."` (today there's no prefix kwarg on those, but `App.mount(module, prefix=...)` exists at `app.py:127-146` — apply the check there too).

4. **`BeforeInvoke` chain.** Add `_before_invoke: list[BeforeInvoke]` on `Module`, with `app.add_before_invoke(hook)`. Empty by default. Plumbed in `invoke` / `invoke_stream` and in the `/_skaal/invoke/<fn>` handler so cloud-invoker calls also see hooks.

5. **Streaming pass-through.** Audit `runtime/middleware.py:ResilientInvoker.__call__` — confirm async-generator returns are not awaited (they should be returned as-is). Add a test (see step 7). Document the contract on `RetryPolicy` that retries apply only up to the first yield.

6. **Update deploy targets.**
    - `skaal/deploy/targets/aws.py` — Cloud Scheduler / EventBridge URLs change from `/<fn>` to `/_skaal/invoke/<qualified_name>`. API Gateway integration for the user's mounted FastAPI is unchanged (it's a catch-all proxy already).
    - `skaal/deploy/targets/gcp.py` — Cloud Scheduler URLs the same; Cloud Run already serves the full ASGI app.

7. **Tests.** New `tests/runtime/test_invoke.py`:
    - `app.invoke(fn, ...)` applies retry/CB/rate-limit (parametrize over each).
    - Bare `await fn(...)` does **not** apply resilience (this is the documented escape hatch — assert it explicitly so we don't regress).
    - `app.invoke_stream(async_gen_fn, ...)` returns an async iterator; its values match the unwrapped generator.
    - First-yield retry: a stream function that raises on first yield retries and succeeds on the second attempt.
    - Mid-stream error propagates without retry.
    - `BeforeInvoke` hook receives the right `InvokeContext`; mutating `ctx.kwargs` rewrites the call.
    - `BeforeInvoke` hook raising short-circuits the invocation.
    - `POST /_skaal/invoke/<fn>` works; `POST /<fn>` returns 404 (breaking change — captured in test).
    - `mount_asgi` rejects `prefix="/_skaal/foo"`.

8. **Refactor [examples/02_todo_api/app.py](../../examples/02_todo_api/app.py)** to a real FastAPI front-end calling Skaal-decorated compute. The example becomes the canonical demonstration of the blessed path: `GET /todos`, `GET /todos/{id}`, `POST /todos`, `DELETE /todos/{id}`, all written in FastAPI, all calling `app.invoke(...)` for the work.

9. **New example `examples/06_fastapi_streaming/app.py`** — FastAPI front-end with an SSE endpoint streaming from an `@app.function` async generator. The smallest possible demo of the LLM use case.

10. **Docs.**
    - New `docs/http.md` — "Skaal does not do HTTP routing. Use FastAPI / Starlette / Litestar via `mount_asgi`. Here is the pattern, here is `invoke`, here is streaming." Linked from the README.
    - Update README quickstart so the first non-trivial example is the FastAPI-mount pattern, not `POST /<fn>`.
    - Note in `docs/user_gaps.md` §B.1 that those items are owned by the user's web framework and link here.

## Open questions resolved

| # | Question                                                       | Pick                                                                                                  |
|---|----------------------------------------------------------------|-------------------------------------------------------------------------------------------------------|
| 1 | Keep `POST /<fn>` as a deprecated alias for one minor version? | No. Alpha software; the breakage is captured in the changelog and the prefix change is mechanical for any user.|
| 2 | `invoke()` accepts the function object only, or also the name? | Both. Function object is the typed path; string is for cloud-invoker / RPC bridges.                   |
| 3 | Where does `BeforeInvoke` live — App or LocalRuntime?          | App (via Module). Hooks are app-level metadata; the runtime instantiates them at boot.                |
| 4 | Should `invoke_stream` retry across yields?                    | No. Retries cover up to the **first** yield; mid-stream errors propagate. Document on `RetryPolicy`.  |
| 5 | Add a `mount_litestar` next to `mount_asgi`?                   | No. `mount_asgi` already covers Litestar (it's ASGI). Document the fact in `docs/http.md`.            |

## Risks and mitigations

- **Users discover `POST /<fn>` doesn't exist anymore.** Mitigation: clear changelog entry; the new path is mechanical (`/_skaal/invoke/<qualified_name>`); local dev still works via `app.invoke(...)` from Python or curl to the new path.
- **Mid-stream resilience is murky.** First-yield retry is the only honest contract for a streaming RPC; document it explicitly so users don't expect mid-stream replay. Tests pin the contract.
- **Bare-call resilience bypass is a footgun.** A user writes `await predict(...)` from a FastAPI handler and silently loses retry. Mitigation: the docs lead with `app.invoke(...)`; a `tests/` example asserts the behavioural difference; the type hint on the user-facing decorator can mark the function as carrying resilience policies, but enforcing "must go through invoke" at runtime is over-engineering — leave it as a documented practice.
- **`RuntimeHandle` lifecycle.** `app.invoke` needs to find the active runtime. Mitigation: `LocalRuntime.__init__` registers itself on the app via a weakref; `app.invoke` raises a clear error if no runtime is active ("call inside a `serve_async` / `run` block, or use `await fn(...)` for unwrapped local invocation").

## Acceptance criteria

- All existing tests pass; the one breaking-change is `POST /<fn>` → `POST /_skaal/invoke/<fn>`, captured in a single test update and the changelog.
- `app.invoke` and `app.invoke_stream` are documented and exercised in `tests/runtime/test_invoke.py`.
- The refactored `02_todo_api` example runs locally as a real FastAPI app, talks to Skaal-decorated compute, and deploys cleanly to AWS via `skaal deploy`.
- The new `06_fastapi_streaming` example runs locally and streams SSE end-to-end.
- `docs/http.md` exists and is linked from the README.
- [user_gaps.md](../user_gaps.md) §B.1 is updated to point at this ADR.
