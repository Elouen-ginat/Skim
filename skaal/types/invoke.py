from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol, TypeAlias, TypeVar

from skaal.types.observability import HeaderMap

T = TypeVar("T")
T_co = TypeVar("T_co", covariant=True)


class AuthClaims(Protocol):
    def get(self, key: str, default: Any | None = None) -> Any: ...


class InvokeContext(Protocol):
    """Read-only metadata for a single invocation attempt."""

    function_name: str
    kwargs: dict[str, Any]
    is_stream: bool
    attempt: int
    headers: HeaderMap
    auth_claims: AuthClaims | None
    auth_subject: str | None
    trace_id: str | None
    span_id: str | None


BeforeInvoke: TypeAlias = Callable[[InvokeContext], Awaitable[None]]


class StreamFn(Protocol[T_co]):
    """Typing helper for ``@app.function`` async generators."""

    def __call__(self, **kwargs: Any) -> AsyncIterator[T_co]: ...
