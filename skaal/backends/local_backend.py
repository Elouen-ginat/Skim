"""Compatibility shim for the canonical LocalMap backend module."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import Any

from skaal.backends.kv.local_map import LocalMap

# Re-export for backward compatibility — canonical location is now skaal.storage.
from skaal.storage import _deserialize, _serialize  # noqa: F401

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


__all__ = ["LocalMap", "_deserialize", "_serialize", "_sync_run"]
