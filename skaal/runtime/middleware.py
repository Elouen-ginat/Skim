"""Resilience middleware: retry, circuit-breaker, bulkhead, rate-limit.

The stack is applied by :func:`resilient_call` in the order chosen by typical
resilience-engineering practice — *outermost first*::

    retry → circuit breaker → bulkhead → rate limit → handler

Rationale:

- **Retry** is outermost: a retried call should re-check the breaker and
  reserve a fresh bulkhead slot / rate-limit token each time.
- **Circuit breaker** short-circuits before we burn a bulkhead slot or a rate
  token on a call we already know is going to fail.
- **Bulkhead** caps concurrency independent of rate — a long-running call
  holds its slot until it returns.
- **Rate limit** is innermost so breaker trips (fast) don't consume tokens.

No third-party dependencies; each primitive is ~30 lines.
"""

from __future__ import annotations

import asyncio
import inspect
import random
import time
from collections.abc import Awaitable
from typing import Any, Callable

from skaal.errors import SkaalUnavailable
from skaal.types.compute import (
    Bulkhead,
    CircuitBreaker,
    Compute,
    RateLimitPolicy,
    RetryPolicy,
)

# ── Circuit breaker ──────────────────────────────────────────────────────────


class _Breaker:
    """Per-function circuit breaker with half-open probe semantics."""

    __slots__ = ("policy", "failures", "opened_at", "_lock")

    def __init__(self, policy: CircuitBreaker) -> None:
        self.policy = policy
        self.failures = 0
        self.opened_at: float | None = None
        self._lock = asyncio.Lock()

    def _state(self) -> str:
        if self.opened_at is None:
            return "closed"
        elapsed_ms = (time.monotonic() - self.opened_at) * 1000
        return "half-open" if elapsed_ms >= self.policy.recovery_timeout_ms else "open"

    async def guard(
        self, call: Callable[[], Awaitable[Any]], fallback: Callable[[], Awaitable[Any]] | None
    ) -> Any:
        state = self._state()
        if state == "open":
            if fallback is not None:
                return await fallback()
            raise SkaalUnavailable("circuit breaker is open")
        try:
            result = await call()
        except Exception:
            async with self._lock:
                self.failures += 1
                if self.failures >= self.policy.failure_threshold:
                    self.opened_at = time.monotonic()
            raise
        else:
            async with self._lock:
                self.failures = 0
                self.opened_at = None
            return result


# ── Bulkhead (bounded concurrency) ───────────────────────────────────────────


class _Bulkhead:
    __slots__ = ("policy", "_sem")

    def __init__(self, policy: Bulkhead) -> None:
        self.policy = policy
        self._sem = asyncio.Semaphore(policy.max_concurrent_calls)

    async def guard(self, call: Callable[[], Awaitable[Any]]) -> Any:
        if self.policy.max_wait_ms <= 0:
            if not self._sem.locked() or self._sem._value > 0:
                async with self._sem:
                    return await call()
            # No slot free and caller opted for fail-fast.
            raise SkaalUnavailable("bulkhead saturated (fail-fast)")
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self.policy.max_wait_ms / 1000.0)
        except asyncio.TimeoutError as exc:
            raise SkaalUnavailable(f"bulkhead wait exceeded {self.policy.max_wait_ms}ms") from exc
        try:
            return await call()
        finally:
            self._sem.release()


# ── Token-bucket rate limiter ────────────────────────────────────────────────


class _TokenBucket:
    """Simple asyncio-safe token bucket; one bucket per scope key."""

    __slots__ = ("rate", "capacity", "tokens", "updated", "_lock")

    def __init__(self, rate: float, capacity: int) -> None:
        self.rate = rate
        self.capacity = max(1, capacity)
        self.tokens: float = float(self.capacity)
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def take(self) -> bool:
        async with self._lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False


class _RateLimiter:
    __slots__ = ("policy", "_buckets")

    def __init__(self, policy: RateLimitPolicy) -> None:
        self.policy = policy
        self._buckets: dict[str, _TokenBucket] = {}

    def _key(self, kwargs: dict[str, Any]) -> str:
        scope = self.policy.scope
        if scope == "global":
            return "__global__"
        if scope == "per-client":
            return str(kwargs.get("client_id") or kwargs.get("client") or "__anon__")
        if scope.startswith("per-key:"):
            arg = scope.split(":", 1)[1]
            return str(kwargs.get(arg, "__missing__"))
        return "__global__"

    async def guard(
        self,
        call: Callable[[], Awaitable[Any]],
        kwargs: dict[str, Any],
    ) -> Any:
        key = self._key(kwargs)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _TokenBucket(self.policy.requests_per_second, self.policy.burst)
            self._buckets[key] = bucket
        if not await bucket.take():
            raise SkaalUnavailable(
                f"rate limit exceeded ({self.policy.requests_per_second}/s, scope={self.policy.scope})"
            )
        return await call()


# ── Retry with jittered backoff ──────────────────────────────────────────────


def _delay_seconds(policy: RetryPolicy, attempt: int) -> float:
    """Return seconds to sleep before *attempt* (1-indexed)."""
    base = policy.base_delay_ms / 1000.0
    cap = policy.max_delay_ms / 1000.0
    if policy.backoff == "fixed":
        raw = base
    elif policy.backoff == "linear":
        raw = base * attempt
    else:  # "exponential"
        raw = base * (2 ** (attempt - 1))
    raw = min(raw, cap)
    # Full jitter — avoids thundering herd on retry storms.
    return random.uniform(0, raw)


async def _with_retry(
    policy: RetryPolicy,
    call: Callable[[], Awaitable[Any]],
) -> Any:
    last: BaseException | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await call()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt >= policy.max_attempts:
                break
            await asyncio.sleep(_delay_seconds(policy, attempt))
    assert last is not None  # for type-checkers
    raise last


# ── Public entry point ───────────────────────────────────────────────────────


class ResilientInvoker:
    """Stateful wrapper around a single handler — owns its breaker / bulkhead
    / rate-limiter instances so state (failure counts, tokens, …) persists
    across invocations.
    """

    __slots__ = ("fn", "compute", "_breaker", "_bulkhead", "_ratelimit", "_fallback")

    def __init__(
        self,
        fn: Callable[..., Any],
        compute: Compute | None,
        fallback_lookup: Callable[[str], Callable[..., Any] | None] | None = None,
    ) -> None:
        self.fn = fn
        self.compute = compute
        self._breaker = (
            _Breaker(compute.circuit_breaker) if compute and compute.circuit_breaker else None
        )
        self._bulkhead = _Bulkhead(compute.bulkhead) if compute and compute.bulkhead else None
        self._ratelimit = (
            _RateLimiter(compute.rate_limit) if compute and compute.rate_limit else None
        )
        self._fallback: Callable[..., Any] | None = None
        if (
            compute
            and compute.circuit_breaker
            and compute.circuit_breaker.fallback
            and fallback_lookup is not None
        ):
            self._fallback = fallback_lookup(compute.circuit_breaker.fallback)

    async def __call__(self, **kwargs: Any) -> Any:
        async def _raw() -> Any:
            if inspect.iscoroutinefunction(self.fn):
                return await self.fn(**kwargs)
            return self.fn(**kwargs)

        # innermost → outermost
        call: Callable[[], Awaitable[Any]] = _raw

        if self._ratelimit is not None:
            _inner = call
            _rate = self._ratelimit

            async def _rl() -> Any:
                return await _rate.guard(_inner, kwargs)

            call = _rl

        if self._bulkhead is not None:
            _inner2 = call
            _bulk = self._bulkhead

            async def _bh() -> Any:
                return await _bulk.guard(_inner2)

            call = _bh

        if self._breaker is not None:
            _inner3 = call
            _fb = self._fallback
            _brk = self._breaker

            async def _fallback_call() -> Any:
                if _fb is None:
                    raise SkaalUnavailable("circuit breaker is open")
                if inspect.iscoroutinefunction(_fb):
                    return await _fb(**kwargs)
                return _fb(**kwargs)

            async def _cb() -> Any:
                return await _brk.guard(_inner3, _fallback_call if _fb else None)

            call = _cb

        if self.compute and self.compute.retry is not None:
            retry = self.compute.retry
            _inner4 = call

            async def _rt() -> Any:
                return await _with_retry(retry, _inner4)

            call = _rt

        return await call()


def wrap_handler(
    fn: Callable[..., Any],
    fallback_lookup: Callable[[str], Callable[..., Any] | None] | None = None,
) -> Callable[..., Awaitable[Any]]:
    """Wrap *fn* with resilience middleware if it declares any policies.

    Returns *fn* unchanged when no policies are attached — the caller can
    blindly ``await wrap_handler(fn)(**kwargs)`` without paying overhead
    for handlers that don't opt-in.
    """
    compute = getattr(fn, "__skaal_compute__", None)
    if not isinstance(compute, Compute) or not (
        compute.retry or compute.circuit_breaker or compute.rate_limit or compute.bulkhead
    ):
        # No policies — return a thin async adapter so the call shape is uniform.
        async def _passthrough(**kwargs: Any) -> Any:
            if inspect.iscoroutinefunction(fn):
                return await fn(**kwargs)
            return fn(**kwargs)

        return _passthrough

    return ResilientInvoker(fn, compute, fallback_lookup)
