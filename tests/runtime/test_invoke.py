from __future__ import annotations

import pytest

from skaal import App, Module, RetryPolicy
from skaal.runtime.local import LocalRuntime


@pytest.mark.asyncio
async def test_app_invoke_applies_retry() -> None:
    app = App("invoke-retry")
    attempts = {"n": 0}

    @app.function(retry=RetryPolicy(max_attempts=3, base_delay_ms=1, max_delay_ms=1))
    async def flaky(name: str) -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("blip")
        return name

    runtime = LocalRuntime(app)
    assert await app.invoke(flaky, name="ok") == "ok"
    assert attempts["n"] == 2
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_bare_function_call_bypasses_resilience() -> None:
    app = App("invoke-bare")
    attempts = {"n": 0}

    @app.function(retry=RetryPolicy(max_attempts=3, base_delay_ms=1, max_delay_ms=1))
    async def flaky() -> str:
        attempts["n"] += 1
        raise RuntimeError("blip")

    with pytest.raises(RuntimeError, match="blip"):
        await flaky()
    assert attempts["n"] == 1


@pytest.mark.asyncio
async def test_app_invoke_stream_retries_first_yield_only() -> None:
    app = App("invoke-stream")
    attempts = {"n": 0}

    @app.function(retry=RetryPolicy(max_attempts=2, base_delay_ms=1, max_delay_ms=1))
    async def stream(prompt: str):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("warmup")
        yield f"{prompt}:1"
        yield f"{prompt}:2"

    runtime = LocalRuntime(app)
    assert [item async for item in app.invoke_stream(stream, prompt="hello")] == [
        "hello:1",
        "hello:2",
    ]
    assert attempts["n"] == 2
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_app_invoke_stream_does_not_retry_mid_stream() -> None:
    app = App("invoke-midstream")
    attempts = {"n": 0}

    @app.function(retry=RetryPolicy(max_attempts=3, base_delay_ms=1, max_delay_ms=1))
    async def stream():
        attempts["n"] += 1
        yield "first"
        raise RuntimeError("mid-stream")

    runtime = LocalRuntime(app)
    seen: list[str] = []
    with pytest.raises(RuntimeError, match="mid-stream"):
        async for item in app.invoke_stream(stream):
            seen.append(item)
    assert seen == ["first"]
    assert attempts["n"] == 1
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_before_invoke_hook_can_rewrite_kwargs() -> None:
    app = App("invoke-hooks")
    seen: list[tuple[str, bool, int]] = []

    @app.function()
    async def greet(name: str) -> str:
        return f"hello {name}"

    @app.add_before_invoke
    async def record_and_patch(ctx):
        seen.append((ctx.function_name, ctx.is_stream, ctx.attempt))
        ctx.kwargs["name"] = ctx.kwargs["name"].upper()

    runtime = LocalRuntime(app)
    assert await app.invoke(greet, name="copilot") == "hello COPILOT"
    assert seen == [("invoke-hooks.greet", False, 1)]
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_before_invoke_hook_can_short_circuit() -> None:
    app = App("invoke-blocked")

    @app.function()
    async def greet(name: str) -> str:
        return f"hello {name}"

    @app.add_before_invoke
    async def reject(ctx):
        raise RuntimeError(f"blocked {ctx.function_name}")

    runtime = LocalRuntime(app)
    with pytest.raises(RuntimeError, match="blocked invoke-blocked.greet"):
        await app.invoke(greet, name="copilot")
    await runtime.shutdown()


def test_mount_rejects_reserved_skaal_prefix() -> None:
    app = App("root")
    child = Module("child")
    with pytest.raises(ValueError, match="reserved"):
        app.mount(child, prefix="/_skaal/internal")
