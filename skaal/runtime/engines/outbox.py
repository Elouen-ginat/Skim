"""Outbox engine — transactional event relay.

User code writes into the outbox via :meth:`Outbox.write`; the engine drains
pending rows and ships them to the configured channel.  The storage write and
outbox-row write happen inside a single :meth:`atomic_update` so success and
publish intent are coupled.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal, cast

from skaal.patterns import Outbox
from skaal.runtime.engines.base import register_engine

DeliveryMode = Literal["at-least-once", "exactly-once"]
DeliveryFinalizer = Callable[[Any, str, dict[str, Any]], Awaitable[None]]


async def _delete_delivered_row(store_backend: Any, key: str, row: dict[str, Any]) -> None:
    await store_backend.delete(key)


async def _mark_delivered_row(store_backend: Any, key: str, row: dict[str, Any]) -> None:
    row["delivered"] = True
    await store_backend.set(key, row)


_DELIVERY_FINALIZERS: dict[DeliveryMode, DeliveryFinalizer] = {
    "at-least-once": _delete_delivered_row,
    "exactly-once": _mark_delivered_row,
}


@register_engine(Outbox)
class OutboxEngine:
    """Background relay that publishes pending outbox rows to a channel."""

    def __init__(self, outbox: Outbox[Any], poll_interval: float = 0.05) -> None:
        self.outbox = outbox
        self.poll_interval = poll_interval
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self, context: Any) -> None:
        # Install a send helper on the outbox so user code has a one-liner:
        #     await orders_outbox.write(key, payload)
        self._stopping = asyncio.Event()
        setattr(self.outbox, "write", self._write_factory())
        self._task = asyncio.create_task(self._relay_loop(), name=f"outbox:{self._outbox_name()}")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    # ── Writer + relay ───────────────────────────────────────────────────────

    def _write_factory(self) -> Any:
        async def write(row_key: str, payload: Any) -> None:
            """Atomically append *payload* to the outbox.

            The payload is stored under ``outbox:<row_key>:<ts>`` so ordering
            is preserved by the backend's lexicographic scan.
            """
            store_backend = _backend_of(self.outbox.storage)
            ts = f"{time.time_ns():020d}"
            key = f"outbox:{row_key}:{ts}"

            def _write(current: Any) -> Any:
                return {"payload": payload, "written_at": ts, "delivered": False}

            await store_backend.atomic_update(key, _write)

        return write

    async def _relay_loop(self) -> None:
        store_backend = _backend_of(self.outbox.storage)
        channel = self.outbox.channel
        try:
            while not self._stopping.is_set():
                try:
                    pending = await store_backend.scan("outbox:")
                except Exception:  # noqa: BLE001
                    pending = []
                delivered_any = False
                for key, row in sorted(pending):
                    if not isinstance(row, dict) or row.get("delivered"):
                        continue
                    row_data = cast(dict[str, Any], row)
                    try:
                        if hasattr(channel, "send"):
                            await channel.send(row_data["payload"])
                        elif hasattr(channel, "append"):
                            await channel.append(row_data["payload"])
                        else:
                            continue
                    except Exception:  # noqa: BLE001
                        # Retry on next tick — at-least-once delivery.
                        continue

                    try:
                        finalizer = _DELIVERY_FINALIZERS[cast(DeliveryMode, self.outbox.delivery)]
                        await finalizer(store_backend, key, row_data)
                    except Exception:  # noqa: BLE001
                        continue
                    delivered_any = True
                if not delivered_any:
                    try:
                        await asyncio.wait_for(self._stopping.wait(), timeout=self.poll_interval)
                    except asyncio.TimeoutError:
                        continue
        except asyncio.CancelledError:
            return

    def _outbox_name(self) -> str:
        return getattr(self.outbox.storage, "__name__", "outbox")


def _backend_of(storage_cls: Any) -> Any:
    """Return the wired backend on a ``@storage`` class.

    ``Store`` classes keep their backend on a class-level
    attribute after ``cls.wire(backend)`` is called.
    """
    for attr in ("_backend", "__skaal_backend__"):
        backend = getattr(storage_cls, attr, None)
        if backend is not None:
            return backend
    raise RuntimeError(
        f"outbox storage {storage_cls!r} has no wired backend — "
        "call cls.wire(backend) before starting the outbox engine"
    )
