"""In-memory storage backend and backward-compat wiring for plain classes."""

from __future__ import annotations

import asyncio

# Re-export for backward compatibility — canonical location is now skaal.storage.
from skaal.storage import _deserialize, _serialize  # noqa: F401
import concurrent.futures
import threading
from typing import Any, List

# ── Sync/async bridge ─────────────────────────────────────────────────────────

# A module-level thread executor used by the sync wrappers when called from
# inside an already-running event loop (e.g. uvicorn / async code paths).
_sync_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="skaal-sync",
)

# A single, persistent event loop that lives in a background daemon thread.
# Used by sync frameworks (Dash/Flask/gunicorn) where there is no running loop
# in the caller's thread.  Keeping one loop alive means asyncpg connection
# pools stay valid across calls — asyncio.run() would close the loop after each
# call, turning every pool stale on the next request.
_bg_loop: asyncio.AbstractEventLoop | None = None
_bg_loop_lock = threading.Lock()


def _get_bg_loop() -> asyncio.AbstractEventLoop:
    global _bg_loop
    with _bg_loop_lock:
        if _bg_loop is None or _bg_loop.is_closed():
            _bg_loop = asyncio.new_event_loop()
            t = threading.Thread(target=_bg_loop.run_forever, daemon=True)
            t.start()
    return _bg_loop


def _sync_run(coro: Any) -> Any:
    """
    Run *coro* from a synchronous context.

    Handles both cases:

    - **No running event loop** (typical in sync frameworks like Dash/Flask /
      gunicorn sync workers): submits to a single persistent background event
      loop that lives in a daemon thread.  The persistent loop keeps connection
      pools (asyncpg, redis, …) alive across calls — ``asyncio.run()`` would
      create *and close* a fresh loop each time, invalidating every pool on the
      very next request.
    - **Running event loop in current thread** (e.g., called from inside
      uvicorn/asyncio code): off-loads to a separate thread with its own loop
      via the thread-pool executor, blocking until done.

    Example (Dash callback)::

        @callback(Output("graph", "figure"), Input("session-id", "data"))
        def update_graph(session_id):
            state = Sessions.sync_get(session_id)  # safe in Dash callbacks
            ...
    """
    try:
        asyncio.get_running_loop()
        # A loop is running in this thread (e.g., uvicorn).
        # Off-load to a dedicated thread that can create its own loop.
        return _sync_executor.submit(asyncio.run, coro).result()
    except RuntimeError:
        # No running loop (gunicorn sync worker, Flask dev server, etc.).
        # Submit to the persistent background loop and block until done.
        loop = _get_bg_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result()


# ── LocalMap ───────────────────────────────────────────────────────────────────


class LocalMap:
    """
    In-memory key-value store that satisfies the :class:`~skaal.backends.base.StorageBackend`
    protocol.

    Used by :class:`~skaal.runtime.local.LocalRuntime` to back storage classes
    during local development and testing.  All methods are async to match the
    production backend interface.
    """

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        import asyncio

        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any | None:
        return self._data.get(key)

    async def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def list(self) -> list[tuple[str, Any]]:
        return list(self._data.items())

    async def scan(self, prefix: str = "") -> List[tuple[str, Any]]:
        return [(k, v) for k, v in self._data.items() if k.startswith(prefix)]

    async def increment_counter(self, key: str, delta: int = 1) -> int:
        """Atomically increment a counter using a lock."""
        async with self._lock:
            current = int(self._data.get(key, 0))
            new_value = current + delta
            self._data[key] = new_value
            return new_value

    async def close(self) -> None:
        pass

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"LocalMap({len(self._data)} keys)"


# ── patch_storage_class ────────────────────────────────────────────────────────


def patch_storage_class(cls: type, backend: Any) -> None:
    """
    Wire *backend* into a storage class.

    For :class:`~skaal.storage.Map` and :class:`~skaal.storage.Collection`
    subclasses this simply sets ``cls._backend = backend`` — the real methods
    live on the class and delegate to ``_backend`` automatically.

    For **plain classes** (``class Counts: pass``) that don't inherit from
    Map/Collection, this injects thin async wrappers as static methods for
    backward compatibility.
    """
    from skaal.storage import Collection, Map

    cls._backend = backend  # type: ignore[attr-defined]

    # Map/Collection subclasses already have real classmethods — done.
    if isinstance(cls, type) and issubclass(cls, (Map, Collection)):
        return

    # ── Plain-class backward compat: inject thin wrappers ─────────────────

    async def _get(key: str) -> Any | None:
        return await backend.get(key)

    async def _set(key: str, value: Any) -> None:
        await backend.set(key, value)

    async def _delete(key: str) -> None:
        await backend.delete(key)

    async def _list() -> list[tuple[str, Any]]:
        return await backend.list()

    async def _scan(prefix: str = "") -> list[tuple[str, Any]]:
        return await backend.scan(prefix)

    async def _close() -> None:
        await backend.close()

    def _sync_get(key: str) -> Any | None:
        return _sync_run(_get(key))

    def _sync_set(key: str, value: Any) -> None:
        _sync_run(_set(key, value))

    def _sync_delete(key: str) -> None:
        _sync_run(_delete(key))

    def _sync_list() -> list[tuple[str, Any]]:
        return _sync_run(_list())

    def _sync_scan(prefix: str = "") -> list[tuple[str, Any]]:
        return _sync_run(_scan(prefix))

    cls.get = staticmethod(_get)  # type: ignore[attr-defined]
    cls.set = staticmethod(_set)  # type: ignore[attr-defined]
    cls.delete = staticmethod(_delete)  # type: ignore[attr-defined]
    cls.list = staticmethod(_list)  # type: ignore[attr-defined]
    cls.scan = staticmethod(_scan)  # type: ignore[attr-defined]
    cls.close = staticmethod(_close)  # type: ignore[attr-defined]
    cls.sync_get = staticmethod(_sync_get)  # type: ignore[attr-defined]
    cls.sync_set = staticmethod(_sync_set)  # type: ignore[attr-defined]
    cls.sync_delete = staticmethod(_sync_delete)  # type: ignore[attr-defined]
    cls.sync_list = staticmethod(_sync_list)  # type: ignore[attr-defined]
    cls.sync_scan = staticmethod(_sync_scan)  # type: ignore[attr-defined]
