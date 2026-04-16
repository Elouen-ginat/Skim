"""Tests for the resilience middleware (retry / breaker / bulkhead / rate-limit)."""

from __future__ import annotations

import asyncio
import time

import pytest

from skaal.errors import SkaalUnavailable
from skaal.runtime.middleware import ResilientInvoker, wrap_handler
from skaal.types.compute import (
    Bulkhead,
    CircuitBreaker,
    Compute,
    RateLimitPolicy,
    RetryPolicy,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _with_compute(fn, compute: Compute):  # type: ignore[no-untyped-def]
    setattr(fn, "__skaal_compute__", compute)
    return fn


# ── Retry ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_recovers_transient_failure() -> None:
    attempts = {"n": 0}

    async def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("blip")
        return "ok"

    fn = _with_compute(
        flaky,
        Compute(retry=RetryPolicy(max_attempts=5, base_delay_ms=1, max_delay_ms=5)),
    )
    wrapped = wrap_handler(fn)
    assert await wrapped() == "ok"
    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_retry_gives_up_and_raises() -> None:
    async def always() -> None:
        raise RuntimeError("nope")

    fn = _with_compute(
        always,
        Compute(retry=RetryPolicy(max_attempts=2, base_delay_ms=1, max_delay_ms=2)),
    )
    wrapped = wrap_handler(fn)
    with pytest.raises(RuntimeError, match="nope"):
        await wrapped()


# ── Circuit breaker ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_breaker_opens_after_threshold() -> None:
    async def always_fails() -> None:
        raise RuntimeError("boom")

    fn = _with_compute(
        always_fails,
        Compute(circuit_breaker=CircuitBreaker(failure_threshold=2, recovery_timeout_ms=10_000)),
    )
    wrapped = wrap_handler(fn)

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await wrapped()

    # Breaker now open — next call should short-circuit with SkaalUnavailable.
    with pytest.raises(SkaalUnavailable):
        await wrapped()


@pytest.mark.asyncio
async def test_breaker_half_open_probe_resets_on_success() -> None:
    state = {"fail": True}

    async def toggled() -> str:
        if state["fail"]:
            raise RuntimeError("x")
        return "ok"

    fn = _with_compute(
        toggled,
        Compute(
            circuit_breaker=CircuitBreaker(failure_threshold=1, recovery_timeout_ms=10),
        ),
    )
    wrapped = wrap_handler(fn)

    with pytest.raises(RuntimeError):
        await wrapped()
    # Let recovery_timeout_ms elapse, then the probe should succeed and reset.
    await asyncio.sleep(0.05)
    state["fail"] = False
    assert await wrapped() == "ok"
    # And subsequent calls continue working.
    assert await wrapped() == "ok"


# ── Bulkhead ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bulkhead_fails_fast_when_saturated() -> None:
    gate = asyncio.Event()

    async def slow() -> str:
        await gate.wait()
        return "done"

    fn = _with_compute(
        slow,
        Compute(bulkhead=Bulkhead(max_concurrent_calls=1, max_wait_ms=10)),
    )
    wrapped = wrap_handler(fn)

    first = asyncio.create_task(wrapped())
    # Let first acquire the slot.
    await asyncio.sleep(0.01)
    with pytest.raises(SkaalUnavailable):
        await wrapped()
    gate.set()
    assert await first == "done"


# ── Rate limiter ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_token_bucket() -> None:
    async def ok() -> str:
        return "ok"

    fn = _with_compute(
        ok,
        Compute(rate_limit=RateLimitPolicy(requests_per_second=1.0, burst=2)),
    )
    wrapped = wrap_handler(fn)

    # Two immediate calls fit within the burst.
    assert await wrapped() == "ok"
    assert await wrapped() == "ok"
    with pytest.raises(SkaalUnavailable):
        await wrapped()


# ── Composition ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wrap_handler_no_policies_is_transparent() -> None:
    async def plain(x: int) -> int:
        return x * 2

    wrapped = wrap_handler(plain)
    assert await wrapped(x=3) == 6


@pytest.mark.asyncio
async def test_fallback_runs_when_breaker_open() -> None:
    async def broken() -> None:
        raise RuntimeError("x")

    async def fallback() -> str:
        return "fallback"

    fn = _with_compute(
        broken,
        Compute(
            circuit_breaker=CircuitBreaker(
                failure_threshold=1, recovery_timeout_ms=60_000, fallback="fallback"
            )
        ),
    )
    wrapped = ResilientInvoker(fn, fn.__skaal_compute__, fallback_lookup=lambda n: fallback)

    with pytest.raises(RuntimeError):
        await wrapped()
    # Breaker now open — fallback wins.
    assert await wrapped() == "fallback"


@pytest.mark.asyncio
async def test_retry_plus_rate_limit_composes() -> None:
    attempts = {"n": 0}

    async def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RuntimeError("blip")
        return "ok"

    fn = _with_compute(
        flaky,
        Compute(
            retry=RetryPolicy(max_attempts=4, base_delay_ms=1, max_delay_ms=2),
            rate_limit=RateLimitPolicy(requests_per_second=1000.0, burst=10),
        ),
    )
    wrapped = wrap_handler(fn)
    t0 = time.monotonic()
    assert await wrapped() == "ok"
    assert time.monotonic() - t0 < 1.0
