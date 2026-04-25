from __future__ import annotations

from typing import TYPE_CHECKING

from skaal.types.runtime import RuntimeApp

if TYPE_CHECKING:
    from skaal.runtime.engines.base import PatternEngine


class _RuntimeLifecycleMixin:
    if TYPE_CHECKING:
        app: RuntimeApp
        _engines: list[PatternEngine]

        async def shutdown(self) -> None: ...

        async def _serve_skaal(self) -> None: ...

    async def serve(self) -> None:
        await self._start_engines()
        try:
            await self._serve_runtime()
        finally:
            await self.shutdown()

    async def _start_engines(self) -> None:
        from skaal.runtime.engines import start_engines_for

        self._engines = await start_engines_for(self.app, self)

    async def _serve_runtime(self) -> None:
        await self._serve_skaal()
