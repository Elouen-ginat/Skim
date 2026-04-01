"""App — a deployable Skim application. Extends Module with a deploy target."""

from __future__ import annotations

from typing import Any, Callable, TypeVar

from skaal.module import Module, ModuleExport

F = TypeVar("F", bound=Callable[..., Any])


class App(Module):
    """
    Central registry for a Skim application.

    ``App`` extends ``Module`` with a deploy target (``deploy()``) and HTTP
    mounting (``mount()``). All storage, agent, function, channel, pattern,
    and attach methods are inherited from ``Module`` — the public API is
    identical to before this refactor.

    Usage::

        app = App("my-service")

        @app.storage(read_latency="< 5ms", durability="persistent")
        class Profiles(Map[str, Profile]):
            pass

        @app.agent(persistent=True)
        class Customer(Agent):
            score: float = 0.0

        @app.function(compute=Compute(latency="< 200ms"))
        async def predict(customer_id: str) -> float:
            ...

        @app.deploy(target="k8s", region="eu-west-1", min_instances=2)
        def main():
            app.serve_http(port=8080)
    """

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._deploy_config: dict[str, Any] = {}

    # ── Deploy ─────────────────────────────────────────────────────────────

    def deploy(
        self,
        *,
        target: str = "k8s",
        region: str | None = None,
        min_instances: int = 1,
        max_instances: int = 10,
        scale_on: str | None = None,
        overflow: str | None = None,
    ) -> Callable[[F], F]:
        """Register the deploy target for this application."""
        from skaal.decorators import deploy as _deploy_dec

        outer = _deploy_dec(
            target=target,
            region=region,
            min_instances=min_instances,
            max_instances=max_instances,
            scale_on=scale_on,
            overflow=overflow,
        )

        def decorator(fn: F) -> F:
            annotated = outer(fn)
            self._deploy_config = annotated.__skim_deploy__  # type: ignore[attr-defined]
            return annotated

        return decorator

    # ── HTTP mounting ──────────────────────────────────────────────────────

    def mount(self, module: Module, *, prefix: str) -> ModuleExport:
        """
        Embed a Module AND map its HTTP-serving functions under a URL prefix.

        Equivalent to ``app.use(module)`` but additionally registers route
        prefix mappings so the deploy engine wires the proxy / API gateway
        correctly.

        Usage::

            app.mount(auth, prefix="/auth")
            # auth's functions are now accessible at /auth/*
        """
        exports = self.use(module)
        # Record the prefix mapping for the deploy engine
        ns = exports.namespace or module.name
        if not hasattr(self, "_mounts"):
            self._mounts: dict[str, str] = {}
        self._mounts[ns] = prefix
        return exports

    # ── Introspection ──────────────────────────────────────────────────────

    def describe(self) -> dict[str, Any]:
        base = super().describe()
        base["deploy"] = self._deploy_config
        base["mounts"] = getattr(self, "_mounts", {})
        return base

    def __repr__(self) -> str:
        return (
            f"App({self.name!r}, "
            f"storage={list(self._storage)}, "
            f"agents={list(self._agents)}, "
            f"functions={list(self._functions)})"
        )
