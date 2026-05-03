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

Tenacity handles retry orchestration and pybreaker owns the circuit-breaker
state machine; bulkhead and rate limiting stay local because the runtime needs
asyncio-native semantics and per-request scope keys.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import random
import time
from collections.abc import AsyncIterator, Awaitable
from typing import Any, Callable

import pybreaker
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception_type, stop_after_attempt

from skaal.errors import SkaalUnavailable
from skaal.types.compute import (
    Bulkhead,
    CircuitBreaker,
    Compute,
    RateLimitPolicy,
    RetryPolicy,
)

# ── Circuit breaker ──────────────────────────────────────────────────────────


class _AsyncCircuitBreaker:
    async def guard(
        self, call: Callable[[], Awaitable[Any]], fallback: Callable[[], Awaitable[Any]] | None
    ) -> Any:
        try:
            with self._breaker.calling():
                return await call()
        except pybreaker.CircuitBreakerError as exc:
            if fallback is not None:
                return await fallback()
            raise SkaalUnavailable("circuit breaker is open") from exc

    __slots__ = ("_breaker",)

    def __init__(self, policy: CircuitBreaker) -> None:
        self._breaker = pybreaker.CircuitBreaker(
            fail_max=policy.failure_threshold,
            reset_timeout=policy.recovery_timeout_ms / 1000.0,
            success_threshold=1,
            throw_new_error_on_trip=False,
        )


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


_current_attempt: contextvars.ContextVar[int] = contextvars.ContextVar(
    "skaal_current_attempt", default=1
)


def _retry_wait(policy: RetryPolicy) -> Callable[[RetryCallState], float]:
    """Build a Tenacity-compatible wait strategy that preserves Skaal jitter semantics."""

    def _wait(retry_state: RetryCallState) -> float:
        attempt = retry_state.attempt_number
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

    return _wait


async def _with_retry(
    policy: RetryPolicy,
    call: Callable[[], Awaitable[Any]],
) -> Any:
    retrying = AsyncRetrying(
        reraise=True,
        stop=stop_after_attempt(policy.max_attempts),
        wait=_retry_wait(policy),
        retry=retry_if_exception_type(Exception),
    )
    async for attempt in retrying:
        token = _current_attempt.set(attempt.retry_state.attempt_number)
        try:
            with attempt:
                return await call()
        finally:
            _current_attempt.reset(token)
    raise AssertionError("retry loop exited without returning or raising")


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
            _AsyncCircuitBreaker(compute.circuit_breaker)
            if compute and compute.circuit_breaker
            else None
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
        return await self.invoke(kwargs=kwargs)

    async def invoke(
        self,
        *,
        kwargs: dict[str, Any] | None = None,
        before_attempt: Callable[[int, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    ) -> Any:
        payload = dict(kwargs or {})

        async def _raw() -> Any:
            call_kwargs = dict(payload)
            if before_attempt is not None:
                call_kwargs = await before_attempt(_current_attempt.get(), call_kwargs)
            if inspect.iscoroutinefunction(self.fn):
                return await self.fn(**call_kwargs)
            return self.fn(**call_kwargs)

        return await self._apply_policies(_raw, payload)

    def invoke_stream(
        self,
        *,
        kwargs: dict[str, Any] | None = None,
        before_attempt: Callable[[int, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    ) -> AsyncIterator[Any]:
        payload = dict(kwargs or {})

        async def _start() -> tuple[bool, Any, AsyncIterator[Any]]:
            call_kwargs = dict(payload)
            if before_attempt is not None:
                call_kwargs = await before_attempt(_current_attempt.get(), call_kwargs)
            if inspect.iscoroutinefunction(self.fn):
                result = await self.fn(**call_kwargs)
            else:
                result = self.fn(**call_kwargs)
            iterator = self._ensure_async_iterator(result)
            try:
                first_item = await anext(iterator)
            except StopAsyncIteration:
                return False, None, iterator
            return True, first_item, iterator

        async def _stream() -> AsyncIterator[Any]:
            has_first, first_item, iterator = await self._apply_policies(_start, payload)
            if has_first:
                yield first_item
            async for item in iterator:
                yield item

        return _stream()

    async def _apply_policies(
        self,
        raw_call: Callable[[], Awaitable[Any]],
        kwargs: dict[str, Any],
    ) -> Any:
        # innermost → outermost
        call: Callable[[], Awaitable[Any]] = raw_call

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

    @staticmethod
    def _ensure_async_iterator(result: Any) -> AsyncIterator[Any]:
        if inspect.isasyncgen(result):
            return result
        if hasattr(result, "__aiter__"):
            return result
        raise TypeError("invoke_stream() requires an async iterator result")


def wrap_handler(
    fn: Callable[..., Any],
    fallback_lookup: Callable[[str], Callable[..., Any] | None] | None = None,
) -> ResilientInvoker:
    """Wrap *fn* with resilience middleware if it declares any policies.

    Returns a callable invoker object for both direct calls and stream calls.
    """
    compute = getattr(fn, "__skaal_compute__", None)
    return ResilientInvoker(fn, compute if isinstance(compute, Compute) else None, fallback_lookup)
