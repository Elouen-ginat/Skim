from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, cast

from skaal.backends.base import StorageBackend
from skaal.types.runtime import (
    AgentsService,
    AsyncClosable,
    BackendOverrides,
    RuntimeApp,
    RuntimeCallable,
    RuntimeInvoker,
    RuntimeObserver,
    StateService,
    StorageClassMap,
)

if TYPE_CHECKING:
    from skaal.runtime.engines.base import PatternEngine


class _RuntimeCoreMixin:
    if TYPE_CHECKING:
        app: RuntimeApp
        _backends: dict[str, AsyncClosable]
        _backend_overrides: BackendOverrides
        _engines: list[PatternEngine]
        _function_cache: dict[str, RuntimeCallable]
        _invokers: dict[str, RuntimeInvoker]
        _stores: StorageClassMap
        agents: AgentsService
        state: StateService
        observer: RuntimeObserver
        _agent_types: dict[str, type[object]]
        sagas: dict[str, object]

    def _collect_storage_classes(self) -> StorageClassMap:
        return {
            qname: obj
            for qname, obj in self.app._collect_all().items()
            if isinstance(obj, type) and hasattr(obj, "__skaal_storage__")
        }

    def _patch_storage_backends(
        self,
    ) -> None:
        from skaal.relational import is_relational_model, wire_relational_model
        from skaal.storage import Store
        from skaal.vector import VectorStore, is_vector_model

        stores = self._collect_storage_classes()
        for qname, obj in stores.items():
            backend = self._backend_overrides.get(qname)
            if backend is None:
                backend = self._backend_overrides.get(obj.__name__)
            if backend is None:
                raise ValueError(
                    f"No backend resolved for storage {qname!r}. "
                    "Provide a runtime plan or backend_overrides that covers every storage class."
                )

            if is_relational_model(obj):
                self._backends[qname] = cast(AsyncClosable, backend)
                wire_relational_model(obj, backend)
                continue

            if is_vector_model(obj) or issubclass(obj, VectorStore):
                self._backends[qname] = cast(AsyncClosable, backend)
                cast(type[VectorStore[Any]], obj).wire(backend)
                continue

            if issubclass(obj, Store):
                self._backends[qname] = cast(AsyncClosable, backend)
                obj.wire(cast(StorageBackend, backend))

        self._stores = stores

    def _wire_local_channels(self) -> None:
        from skaal.backends.channels.local import wire_local
        from skaal.channel import Channel as SkaalChannel

        for obj in self.app._collect_all().values():
            if isinstance(obj, SkaalChannel):
                wire_local(obj)

    def _collect_functions(self) -> dict[str, RuntimeCallable]:
        funcs: dict[str, RuntimeCallable] = {
            qname: obj
            for qname, obj in self.app._collect_all().items()
            if callable(obj) and hasattr(obj, "__skaal_compute__")
        }
        for name, fn in self.app._functions.items():
            if callable(fn):
                funcs.setdefault(name, cast(RuntimeCallable, fn))
        for name, fn in self.app._schedules.items():
            if callable(fn):
                funcs.setdefault(name, cast(RuntimeCallable, fn))
        return funcs

    def _collect_agent_classes(self) -> dict[str, type[object]]:
        return {
            qname: obj
            for qname, obj in self.app._collect_all().items()
            if isinstance(obj, type) and hasattr(obj, "__skaal_agent__")
        }

    def _initialize_runtime_state(self) -> None:
        from skaal.runtime.middleware import wrap_handler

        self._function_cache = self._collect_functions()
        self._invokers: dict[str, RuntimeInvoker] = {
            name: wrap_handler(fn, fallback_lookup=self._function_cache.get)
            for name, fn in self._function_cache.items()
        }
        self._engines = []
        self.sagas = {}
        self._stores = self._collect_storage_classes()
        self._agent_types = self._collect_agent_classes()
        for qname, agent_cls in self._agent_types.items():
            self.agents.declare(qname, agent_cls)

    @property
    def functions(self) -> dict[str, RuntimeCallable]:
        return self._function_cache

    @property
    def stores(self) -> StorageClassMap:
        return self._stores

    async def shutdown(self) -> None:
        import contextlib

        seen_backends: set[int] = set()

        async def _close_backend(backend: object) -> None:
            marker = id(backend)
            if marker in seen_backends:
                return
            seen_backends.add(marker)

            close = getattr(backend, "close", None)
            if close is None:
                return

            with contextlib.suppress(Exception):
                result = close()
                if inspect.isawaitable(result):
                    await result

        for engine in self._engines:
            with contextlib.suppress(Exception):
                await engine.stop()
        self._engines = []

        for backend in self._backends.values():
            await _close_backend(backend)

        for override_backend in self._backend_overrides.values():
            await _close_backend(override_backend)
