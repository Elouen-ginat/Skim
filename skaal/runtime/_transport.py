from __future__ import annotations

from typing import TYPE_CHECKING

from skaal.types.runtime import DispatchResult, RuntimeApp, RuntimeCallable


class _RuntimeHttpTransportMixin:
    if TYPE_CHECKING:
        app: RuntimeApp
        host: str
        port: int
        _function_cache: dict[str, RuntimeCallable]

        async def _dispatch(self, method: str, path: str, body: bytes) -> DispatchResult: ...

    def _runtime_label(self) -> str:
        return "local"

    def _banner_lines(self, public_fns: list[str]) -> list[str]:
        return [f"    POST /{name}" for name in public_fns]

    async def _serve_skaal(self) -> None:
        try:
            import uvicorn
            from starlette.applications import Starlette
            from starlette.requests import Request as StarletteRequest
            from starlette.responses import JSONResponse
            from starlette.routing import Route
        except ImportError as exc:
            raise RuntimeError(
                f"Skaal {self._runtime_label()} runtime requires uvicorn and starlette.\n"
                "Install them with:  pip install uvicorn starlette\n"
                f"Missing: {exc}"
            ) from exc

        funcs = self._function_cache
        public_fns = [n for n in sorted(funcs) if not hasattr(funcs[n], "__skaal_schedule__")]

        print(f"\n  Skaal {self._runtime_label()} runtime — {self.app.name}")
        print(f"  http://{self.host}:{self.port}\n")
        for line in self._banner_lines(public_fns):
            if line:
                print(line)
            else:
                print()
        print()

        async def _handle(request: StarletteRequest) -> JSONResponse:
            body = await request.body()
            result, status = await self._dispatch(request.method, request.url.path, body)
            return JSONResponse(result, status_code=status)

        asgi_app = Starlette(
            routes=[
                Route("/", _handle, methods=["GET"]),
                Route("/health", _handle, methods=["GET"]),
                Route("/{path:path}", _handle, methods=["GET", "POST"]),
            ]
        )

        config = uvicorn.Config(asgi_app, host=self.host, port=self.port, log_level="info")
        await uvicorn.Server(config).serve()
