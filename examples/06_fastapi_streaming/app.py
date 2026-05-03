"""
FastAPI streaming — mount FastAPI and stream a Skaal async generator via SSE.

Run locally:

    pip install "skaal[examples]"
    skaal run examples.fastapi_streaming:app

Then try:

    curl -N 'http://localhost:8000/chat?prompt=hello%20streaming%20world'
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from skaal import App, RetryPolicy

app = App("fastapi-streaming")
api = FastAPI(title="Skaal FastAPI Streaming")


@app.function(retry=RetryPolicy(max_attempts=2, base_delay_ms=10, max_delay_ms=25))
async def stream_tokens(prompt: str):
    for token in prompt.split():
        await asyncio.sleep(0.02)
        yield f"data: {token}\n\n"
    yield "data: [done]\n\n"


@api.get("/chat")
async def chat(prompt: str) -> StreamingResponse:
    return StreamingResponse(
        app.invoke_stream(stream_tokens, prompt=prompt),
        media_type="text/event-stream",
    )


app.mount_asgi(api, attribute="api")
