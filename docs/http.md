# HTTP with Skaal

Skaal does not try to be a web framework. `@app.function()` is a compute primitive plus a resilience boundary; the public HTTP surface belongs to your mounted ASGI app.

Use FastAPI, Starlette, or Litestar via `app.mount_asgi(...)` and call Skaal compute through `app.invoke(...)` or `app.invoke_stream(...)`.

```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from skaal import App, RetryPolicy

app = App("api")
api = FastAPI()


@app.function(retry=RetryPolicy(max_attempts=3))
async def predict(features: dict) -> dict:
    return {"ok": True, "features": features}


@app.function()
async def stream_tokens(prompt: str):
    for token in prompt.split():
        yield f"data: {token}\n\n"


@api.get("/items/{item_id}")
async def get_item(item_id: str) -> dict:
    return await app.invoke(predict, features={"id": item_id})


@api.get("/chat")
async def chat(prompt: str) -> StreamingResponse:
    return StreamingResponse(
        app.invoke_stream(stream_tokens, prompt=prompt),
        media_type="text/event-stream",
    )


app.mount_asgi(api, attribute="api")
```

Rules of thumb:

- Use `await app.invoke(...)` from your FastAPI or Starlette handlers when you want Skaal retry, circuit-breaker, rate-limit, or bulkhead policies to apply.
- Use `app.invoke_stream(...)` for async-generator functions and hand the returned async iterator to `StreamingResponse`.
- Calling the decorated function directly, like `await predict(...)`, is still allowed for local code paths but bypasses the resilience middleware.
- The Skaal runtime reserves `/_skaal/*` for internal invoker traffic. The internal compute endpoint is `POST /_skaal/invoke/<qualified_function_name>`.
- Mounted user apps own every path outside `/_skaal/*` plus their own middleware, auth, validation, and OpenAPI generation.

Examples:

- `examples.todo_api:app` mounts FastAPI over Skaal compute for a CRUD API.
- `examples.fastapi_streaming:app` streams SSE from a Skaal async generator.
