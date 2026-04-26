from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import Any, List

from skaal.backends._spec import BackendSpec, Wiring
from skaal.deploy.kinds import StorageKind

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


def _sync_run(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
        return _sync_executor.submit(asyncio.run, coro).result()
    except RuntimeError:
        loop = _get_bg_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result()


class LocalMap:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
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
        return [(key, value) for key, value in self._data.items() if key.startswith(prefix)]

    async def increment_counter(self, key: str, delta: int = 1) -> int:
        async with self._lock:
            current = int(self._data.get(key, 0))
            new_value = current + delta
            self._data[key] = new_value
            return new_value

    async def atomic_update(self, key: str, fn: Any) -> Any:
        async with self._lock:
            current = self._data.get(key)
            updated = fn(current)
            self._data[key] = updated
            return updated

    async def close(self) -> None:
        return None

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"LocalMap({len(self._data)} keys)"


LOCAL_MAP_SPEC = BackendSpec(
    name="local-map",
    kinds=frozenset({StorageKind.KV}),
    wiring=Wiring(
        class_name="LocalMap",
        module="skaal.backends.kv.local_map",
    ),
    supported_targets=frozenset({"local"}),
)

__all__ = ["LOCAL_MAP_SPEC", "LocalMap", "_sync_run"]
