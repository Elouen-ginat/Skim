"""Public sync/async bridge helpers for synchronous frameworks and tests."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")

_sync_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="skaal-sync",
)
_bg_loop: asyncio.AbstractEventLoop | None = None
_bg_loop_lock = threading.Lock()


def _get_bg_loop() -> asyncio.AbstractEventLoop:
    global _bg_loop
    with _bg_loop_lock:
        if _bg_loop is None or _bg_loop.is_closed():
            _bg_loop = asyncio.new_event_loop()
            thread = threading.Thread(target=_bg_loop.run_forever, daemon=True)
            thread.start()
    return _bg_loop


async def _await_result(awaitable: Awaitable[T]) -> T:
    return await awaitable


def _run_in_new_loop(awaitable: Awaitable[T]) -> T:
    return asyncio.run(_await_result(awaitable))


def run(awaitable: Awaitable[T]) -> T:
    """Run an awaitable from synchronous code without exposing backend internals."""
    try:
        asyncio.get_running_loop()

        def _submit_bound() -> T:
            return _run_in_new_loop(awaitable)

        return _sync_executor.submit(_submit_bound).result()
    except RuntimeError:
        loop = _get_bg_loop()
        future = asyncio.run_coroutine_threadsafe(_await_result(awaitable), loop)
        return future.result()


__all__ = ["run"]
