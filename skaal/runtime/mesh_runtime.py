"""MeshRuntime — distributed Skaal runtime backed by the Rust ``skaal_mesh`` extension.

Requires the ``mesh`` extra::

    pip install "skaal[mesh]"

If you are editing the Rust crate locally::

    make build-dev

Usage::

    runtime = MeshRuntime(app, plan_json=plan.model_dump_json())
    asyncio.run(runtime.serve())

Or via the CLI::

    skaal run examples.counter:app --distributed
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, cast

from skaal.runtime.base import _SKAAL_INVOKE_PREFIX, BaseRuntime

if TYPE_CHECKING:
    import httpx

    from skaal.runtime.telemetry import RuntimeTelemetry
    from skaal.types import TelemetryConfig

_MAX_BODY_SIZE = 10 * 1024 * 1024
log = logging.getLogger("skaal.runtime")


def _format_banner(title: str, lines: list[str]) -> str:
    return "\n".join(["", title, *lines, ""])


class MeshRuntime(BaseRuntime):
    """Distributed runtime that delegates agent routing and channels to the Rust mesh.

    The HTTP server / dispatch path mirrors :class:`~skaal.runtime.local.LocalRuntime`
    so existing Starlette / uvicorn plumbing is reused.  The mesh layer adds:

    - **Agent placement**: :meth:`route_agent` wraps
      ``SkaalMesh.route_agent_call`` so agent calls resolve to a live instance.
    - **Distributed channels**: :meth:`channel_publish` forwards to
      ``SkaalMesh.publish`` (``tokio::sync::broadcast``-backed pub/sub).
    - **Health**: :meth:`health` returns the mesh's aggregated health snapshot
      (agents, state, migrations, channels).
    """

    def __init__(
        self,
        app: Any,
        *,
        host: str = "127.0.0.1",
        port: int = 8000,
        plan_json: str = "",
        node_id: str | None = None,
        backend_overrides: dict[str, Any] | None = None,
        telemetry: "TelemetryConfig | None" = None,
        telemetry_runtime: "RuntimeTelemetry | None" = None,
        auth_http_client: "httpx.AsyncClient | None" = None,
    ) -> None:
        try:
            import skaal_mesh
        except ImportError as exc:
            raise ImportError(
                "MeshRuntime requires the skaal_mesh native extension.\n"
                'Install it with: pip install "skaal[mesh]"\n'
                "If you are editing the Rust crate locally, run: make build-dev"
            ) from exc

        if plan_json:
            plan_dict = json.loads(plan_json)
            plan_dict.setdefault("app_name", app.name)
            plan_json = json.dumps(plan_dict)
        self._mesh = cast(Any, skaal_mesh).SkaalMesh(app.name, plan_json)
        super().__init__(
            app,
            host=host,
            port=port,
            backend_overrides=backend_overrides,
            telemetry=telemetry,
            telemetry_runtime=telemetry_runtime,
            auth_http_client=auth_http_client,
        )

    def _default_kv_backend(self, namespace: str) -> Any:
        from skaal.backends.local_backend import LocalMap

        del namespace
        return LocalMap()

    def _default_relational_backend(self, namespace: str) -> Any:
        from skaal.backends.sqlite_backend import SqliteBackend

        return SqliteBackend("skaal_local.db", namespace=namespace)

    def _default_vector_backend(self, namespace: str) -> Any:
        from skaal.backends.chroma_backend import ChromaVectorBackend

        return ChromaVectorBackend("skaal_chroma", namespace=namespace)

    def _default_blob_backend(self, namespace: str) -> Any:
        from skaal.backends.file_blob_backend import FileBlobBackend

        return FileBlobBackend(".skaal/blobs", namespace=namespace)

    def _wire_channel_instance(self, channel_obj: Any) -> None:
        from skaal.channel import wire_local

        wire_local(channel_obj)

    def _root_payload(self) -> dict[str, Any]:
        public = sorted(self._public_functions())
        return {
            "app": self.app.name,
            "endpoints": [
                {"path": f"{_SKAAL_INVOKE_PREFIX}{name}", "function": name} for name in public
            ],
            "storage": list(self._backends.keys()),
            "mesh": json.loads(self._mesh.health_snapshot()),
        }

    def _health_payload(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "app": self.app.name,
            "mesh": json.loads(self._mesh.health_snapshot()),
        }

    def _augment_readiness_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload["mesh"] = json.loads(self._mesh.health_snapshot())
        return payload

    # ── Serve (mirrors LocalRuntime._serve_skaal) ─────────────────────────────

    async def serve(self) -> None:
        await self.ensure_started()
        try:
            await self._serve_skaal()
        finally:
            await self.shutdown()

    async def _serve_skaal(self) -> None:
        try:
            import uvicorn
            from starlette.applications import Starlette
            from starlette.requests import Request as StarletteRequest
            from starlette.responses import JSONResponse
            from starlette.routing import Route
        except ImportError as exc:
            raise RuntimeError(
                "MeshRuntime requires uvicorn and starlette.\n"
                "Install them with:  pip install uvicorn starlette\n"
                f"Missing: {exc}"
            ) from exc

        public_fns = sorted(self._public_functions())
        banner_lines = [f"  http://{self.host}:{self.port}", ""]
        for name in public_fns:
            banner_lines.append(f"    POST {_SKAAL_INVOKE_PREFIX}{name}")
        log.info(_format_banner(f"  Skaal mesh runtime — {self.app.name}", banner_lines))

        async def _handle(request: StarletteRequest) -> JSONResponse:
            body = await request.body()
            result, status = await self._dispatch(
                request.method,
                request.url.path,
                body,
                headers=dict(request.headers.items()),
            )
            return JSONResponse(result, status_code=status)

        asgi_app = Starlette(
            routes=[
                Route("/", _handle, methods=["GET"]),
                Route("/health", _handle, methods=["GET"]),
                Route("/ready", _handle, methods=["GET"]),
                Route("/{path:path}", _handle, methods=["GET", "POST"]),
            ]
        )

        config = uvicorn.Config(asgi_app, host=self.host, port=self.port, log_level="info")
        await uvicorn.Server(config).serve()

    # ── Mesh bridge API (used by engines / agents) ────────────────────────────

    def route_agent(self, agent_type: str, agent_id: str, method: str, args: dict[str, Any]) -> str:
        return self._mesh.route_agent_call(agent_type, agent_id, method, json.dumps(args))

    def channel_publish(self, topic: str, message: Any) -> int:
        return self._mesh.publish(topic, json.dumps(message))

    def health(self) -> dict[str, Any]:
        return json.loads(self._mesh.health_snapshot())
