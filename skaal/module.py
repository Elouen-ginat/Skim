"""Module — reusable, composable Skaal fragment. Base class for App."""

from __future__ import annotations

# TYPE_CHECKING import to avoid circular deps at runtime
from typing import TYPE_CHECKING, Any, Callable, TypeVar, overload

from skaal.types import (
    AccessPattern,
    Bulkhead,
    CircuitBreaker,
    Compute,
    DecommissionPolicy,
    Durability,
    Latency,
    RateLimitPolicy,
    RetryPolicy,
    Scale,
    Throughput,
)

if TYPE_CHECKING:
    from skaal.schedule import Cron, Every

F = TypeVar("F", bound=Callable[..., Any])
C = TypeVar("C", bound=type)


class ModuleExport:
    """
    Typed handle for symbols exported by a Module.

    Returned by ``module.export(...)``. Carries references to the exported
    storage classes, agents, functions, and channels so that mounting apps
    can use them as direct Python references.

    Usage::

        exports = auth.export(User, Sessions)
        # In mounting app:
        user = await exports.agents["User"](user_id)
    """

    def __init__(
        self,
        storage: dict[str, Any],
        agents: dict[str, Any],
        functions: dict[str, Any],
        channels: dict[str, Any],
        namespace: str,
    ) -> None:
        self.storage = storage
        self.agents = agents
        self.functions = functions
        self.channels = channels
        self.namespace = namespace

    def __repr__(self) -> str:
        return (
            f"ModuleExport(namespace={self.namespace!r}, "
            f"storage={list(self.storage)}, "
            f"agents={list(self.agents)}, "
            f"functions={list(self.functions)})"
        )


class Module:
    """
    A reusable, composable Skaal fragment.

    A Module can declare storage, agents, functions, channels, and patterns —
    but it has no deploy target. Modules are mounted into Apps (or other Modules)
    via ``app.use(module)``, namespacing their resources automatically.

    Modules are published as pip packages (convention: ``skaal-<name>``). They
    expose their public API via ``module.export(...)``.

    Usage::

        auth = Module("auth")

        @auth.storage(read_latency="< 5ms", durability="persistent")
        class Sessions(skaal.Store[Session]):
            pass

        @auth.agent(persistent=True)
        class User(Agent):
            email: str = ""

        exports = auth.export(Sessions, User)
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._storage: dict[str, Any] = {}
        self._agents: dict[str, Any] = {}
        self._functions: dict[str, Any] = {}
        self._channels: dict[str, Any] = {}
        self._patterns: dict[str, Any] = {}
        self._components: dict[str, Any] = {}
        self._schedules: dict[str, Any] = {}
        self._exports: set[str] = set()
        self._submodules: dict[str, Module] = {}  # namespace → mounted module

    # ── Registration decorators ────────────────────────────────────────────

    @overload
    def storage(
        self,
        cls_to_decorate: C,
        *,
        read_latency: Latency | str | None = ...,
        write_latency: Latency | str | None = ...,
        durability: Durability | str = ...,
        size_hint: str | None = ...,
        access_pattern: AccessPattern | str = ...,
        write_throughput: Throughput | str | None = ...,
        residency: str | None = ...,
        retention: str | None = ...,
        auto_optimize: bool = ...,
        decommission_policy: DecommissionPolicy | None = ...,
        collocate_with: str | None = ...,
    ) -> C: ...

    @overload
    def storage(
        self,
        cls_to_decorate: None = ...,
        *,
        read_latency: Latency | str | None = ...,
        write_latency: Latency | str | None = ...,
        durability: Durability | str = ...,
        size_hint: str | None = ...,
        access_pattern: AccessPattern | str = ...,
        write_throughput: Throughput | str | None = ...,
        residency: str | None = ...,
        retention: str | None = ...,
        auto_optimize: bool = ...,
        decommission_policy: DecommissionPolicy | None = ...,
        collocate_with: str | None = ...,
    ) -> Callable[[C], C]: ...

    def storage(
        self,
        cls_to_decorate: C | None = None,
        *,
        read_latency: Latency | str | None = None,
        write_latency: Latency | str | None = None,
        durability: Durability | str = Durability.PERSISTENT,
        size_hint: str | None = None,
        access_pattern: AccessPattern | str = AccessPattern.RANDOM_READ,
        write_throughput: Throughput | str | None = None,
        residency: str | None = None,
        retention: str | None = None,
        auto_optimize: bool = False,
        decommission_policy: DecommissionPolicy | None = None,
        collocate_with: str | None = None,
    ) -> C | Callable[[C], C]:
        """Register a storage class with infrastructure constraints.

        Can be used as:
            @app.storage
            class MyStorage: ...

        Or:
            @app.storage(read_latency="< 100ms")
            class MyStorage: ...
        """
        from skaal.decorators import storage as _storage_dec

        outer = _storage_dec(
            read_latency=read_latency,
            write_latency=write_latency,
            durability=durability,
            size_hint=size_hint,
            access_pattern=access_pattern,
            write_throughput=write_throughput,
            residency=residency,
            retention=retention,
            auto_optimize=auto_optimize,
            decommission_policy=decommission_policy,
            collocate_with=collocate_with,
        )

        def decorator(cls: C) -> C:
            annotated = outer(cls)
            self._storage[cls.__name__] = annotated
            return annotated

        if cls_to_decorate is None:
            return decorator
        return decorator(cls_to_decorate)

    @overload
    def agent(self, cls_to_decorate: C, *, persistent: bool = ...) -> C: ...

    @overload
    def agent(self, cls_to_decorate: None = ..., *, persistent: bool = ...) -> Callable[[C], C]: ...

    def agent(
        self, cls_to_decorate: C | None = None, *, persistent: bool = True
    ) -> C | Callable[[C], C]:
        """Register an agent class.

        Can be used as:
            @app.agent
            class MyAgent: ...

        Or:
            @app.agent(persistent=True)
            class MyAgent: ...
        """
        from skaal.agent import agent as _agent_dec

        outer = _agent_dec(persistent=persistent)

        def decorator(cls: C) -> C:
            annotated = outer(cls)
            self._agents[cls.__name__] = annotated
            return annotated

        if cls_to_decorate is None:
            return decorator
        return decorator(cls_to_decorate)

    @overload
    def function(
        self,
        fn_to_decorate: F,
        *,
        compute: Compute | None = ...,
        scale: Scale | None = ...,
        retry: RetryPolicy | None = ...,
        circuit_breaker: CircuitBreaker | None = ...,
        rate_limit: RateLimitPolicy | None = ...,
        bulkhead: Bulkhead | None = ...,
    ) -> F: ...

    @overload
    def function(
        self,
        fn_to_decorate: None = ...,
        *,
        compute: Compute | None = ...,
        scale: Scale | None = ...,
        retry: RetryPolicy | None = ...,
        circuit_breaker: CircuitBreaker | None = ...,
        rate_limit: RateLimitPolicy | None = ...,
        bulkhead: Bulkhead | None = ...,
    ) -> Callable[[F], F]: ...

    def function(
        self,
        fn_to_decorate: F | None = None,
        *,
        compute: Compute | None = None,
        scale: Scale | None = None,
        retry: RetryPolicy | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        rate_limit: RateLimitPolicy | None = None,
        bulkhead: Bulkhead | None = None,
    ) -> F | Callable[[F], F]:
        """Register a compute function with optional constraints and resilience policies.

        Can be used as:
            @app.function
            def my_func(): ...

        Or:
            @app.function(compute=...)
            def my_func(): ...
        """

        def decorator(fn: F) -> F:
            _compute = compute or Compute()
            if retry is not None:
                _compute.retry = retry
            if circuit_breaker is not None:
                _compute.circuit_breaker = circuit_breaker
            if rate_limit is not None:
                _compute.rate_limit = rate_limit
            if bulkhead is not None:
                _compute.bulkhead = bulkhead
            setattr(fn, "__skaal_compute__", _compute)
            if scale is not None:
                setattr(fn, "__skaal_scale__", scale)
            self._functions[fn.__name__] = fn
            return fn

        if fn_to_decorate is None:
            return decorator
        return decorator(fn_to_decorate)

    def channel(
        self,
        *,
        buffer: int = 1000,
        throughput: Throughput | str | None = None,
        durability: Durability = Durability.PERSISTENT,
    ) -> Callable[[C], C]:
        """
        Register a Channel subclass as a named, constraint-bearing resource.

        Usage::

            @auth.channel(throughput="> 500 events/s", durability="durable")
            class UserEvents(Channel[UserEvent]):
                pass
        """
        if isinstance(throughput, str):
            throughput = Throughput(throughput)
        if isinstance(durability, str):
            durability = Durability(durability)

        def decorator(cls: C) -> C:
            setattr(
                cls,
                "__skaal_channel__",
                {
                    "buffer": buffer,
                    "throughput": throughput,
                    "durability": durability,
                },
            )
            # Store an instance (not the class) so the runtime can wire it with
            # wire_local / wire_redis.  The class is still returned so type
            # annotations remain valid and the solver can resolve it by name.
            instance = cls(buffer=buffer)
            self._channels[cls.__name__] = instance
            return cls

        return decorator

    def attach(self, component: Any) -> Any:
        """Attach an external or provisioned component to this module."""
        self._components[component.name] = component
        return component

    @overload
    def schedule(
        self,
        fn_to_decorate: F,
        *,
        trigger: "Every | Cron",
        emit_to: Any | None = ...,
        timezone: str = ...,
    ) -> F: ...

    @overload
    def schedule(
        self,
        fn_to_decorate: None = ...,
        *,
        trigger: "Every | Cron",
        emit_to: Any | None = ...,
        timezone: str = ...,
    ) -> Callable[[F], F]: ...

    def schedule(
        self,
        fn_to_decorate: F | None = None,
        *,
        trigger: "Every | Cron",
        emit_to: Any | None = None,
        timezone: str = "UTC",
    ) -> F | Callable[[F], F]:
        """Register a background function triggered on a time-based schedule.

        The appropriate cloud scheduler is provisioned automatically:
        - **AWS**: EventBridge rule + Lambda permission
        - **GCP**: Cloud Scheduler job → Cloud Run
        - **Local**: APScheduler ``AsyncIOScheduler``

        Can be used as::

            @app.schedule(trigger=Every(interval="5m"))
            async def cleanup(): ...

            @app.schedule(trigger=Cron(expression="0 8 * * *"), timezone="US/Eastern")
            async def daily_report(ctx: ScheduleContext) -> None:
                print(f"Fired at {ctx.fired_at}")

        Args:
            trigger:   :class:`~skaal.schedule.Every` or :class:`~skaal.schedule.Cron`.
            emit_to:   Optional Channel / EventLog to publish non-``None`` results to.
            timezone:  IANA timezone string (default: ``"UTC"``).
        """
        from skaal.components import ScheduleTrigger

        def decorator(fn: F) -> F:
            setattr(
                fn,
                "__skaal_schedule__",
                {
                    "trigger": trigger,
                    "emit_to": emit_to,
                    "timezone": timezone,
                },
            )
            self._schedules[fn.__name__] = fn
            st = ScheduleTrigger(
                f"{fn.__name__}-schedule",
                trigger=trigger,
                target_function=fn.__name__,
                timezone=timezone,
                emit_to=emit_to.name if emit_to is not None else None,
            )
            self._components[st.name] = st
            return fn

        if fn_to_decorate is None:
            return decorator
        return decorator(fn_to_decorate)

    def pattern(self, p: Any) -> Any:
        """
        Register a pattern (EventLog, Projection, Saga, Outbox) with this module.

        Usage::

            auth.pattern(UserEventLog)
        """
        name = getattr(p, "name", None) or getattr(p, "__class__", type(p)).__name__
        self._patterns[name] = p
        return p

    # ── Export / import API ────────────────────────────────────────────────

    def export(self, *symbols: Any) -> ModuleExport:
        """
        Mark symbols as importable by mounting apps.

        Symbols must be storage classes, agent classes, functions, or channels
        already registered with this module. Raises ``ValueError`` if an
        unregistered symbol is passed.

        Returns a ``ModuleExport`` handle for cross-module references.
        """
        registered: dict[str, dict[str, Any]] = {
            "storage": self._storage,
            "agents": self._agents,
            "functions": self._functions,
            "channels": self._channels,
        }

        exp_storage: dict[str, Any] = {}
        exp_agents: dict[str, Any] = {}
        exp_functions: dict[str, Any] = {}
        exp_channels: dict[str, Any] = {}

        for sym in symbols:
            sym_name = getattr(sym, "__name__", repr(sym))
            found = False
            for bucket_name, bucket in registered.items():
                if sym_name in bucket:
                    self._exports.add(sym_name)
                    found = True
                    if bucket_name == "storage":
                        exp_storage[sym_name] = sym
                    elif bucket_name == "agents":
                        exp_agents[sym_name] = sym
                    elif bucket_name == "functions":
                        exp_functions[sym_name] = sym
                    elif bucket_name == "channels":
                        exp_channels[sym_name] = sym
                    break
            if not found:
                raise ValueError(
                    f"{sym_name!r} is not registered with module {self.name!r}. "
                    f"Register it with @{self.name}.storage / .agent / .function / .channel first."
                )

        return ModuleExport(
            storage=exp_storage,
            agents=exp_agents,
            functions=exp_functions,
            channels=exp_channels,
            namespace=self.name,
        )

    def use(
        self,
        module: Module,
        *,
        namespace: str | None = None,
        share_storage: list[str] | None = None,
    ) -> ModuleExport:
        """
        Mount a Module into this Module, namespacing its resources.

        Args:
            module:        The Module to mount.
            namespace:     Override the module's name as prefix.
                           Default: ``module.name``.
                           Pass ``None`` to merge into root namespace
                           (collision-checked).
            share_storage: Names of storage in *module* to also register
                           under the parent namespace. Must be in module's
                           exports.

        Returns the module's ``ModuleExport``.

        Namespace behaviour::

            app.use(auth)                     # → "auth.Sessions", "auth.User"
            app.use(auth, namespace="id")     # → "id.Sessions", "id.User"
            app.use(auth, namespace=None)     # → "Sessions", "User" (collision-checked)
        """
        ns = namespace if namespace is not None else module.name
        if ns in self._submodules:
            raise ValueError(
                f"Namespace {ns!r} is already occupied by another module. "
                "Pass a different namespace= to app.use()."
            )
        if ns is not None:
            self._submodules[ns] = module
        else:
            # Merge — check for collisions
            for bucket in (self._storage, self._agents, self._functions, self._channels):
                for key in module._exports:
                    if key in bucket:
                        raise ValueError(
                            f"Cannot merge module {module.name!r} into root namespace: "
                            f"{key!r} already registered. Use namespace=<name> instead."
                        )
            self._submodules[""] = module

        # Build exports for only the exported symbols
        exp_storage = {k: v for k, v in module._storage.items() if k in module._exports}
        exp_agents = {k: v for k, v in module._agents.items() if k in module._exports}
        exp_functions = {k: v for k, v in module._functions.items() if k in module._exports}
        exp_channels = {k: v for k, v in module._channels.items() if k in module._exports}

        return ModuleExport(
            storage=exp_storage,
            agents=exp_agents,
            functions=exp_functions,
            channels=exp_channels,
            namespace=ns or "",
        )

    # ── Solver support ─────────────────────────────────────────────────────

    def _collect_all(self) -> dict[str, Any]:
        """
        Recursively collect all registered resources from this module and all
        mounted submodules, applying namespace prefixes.

        Returns a flat dict of ``{qualified_name: annotated_class_or_fn}``.
        Called by the solver via ``App._collect_all()``.

        Examples::

            # Module "auth" with storage "Sessions" mounted under namespace "auth":
            {"auth.Sessions": <class Sessions>, "auth.User": <class User>}
        """
        result: dict[str, Any] = {}
        prefix = f"{self.name}." if self.name else ""

        for name, obj in self._storage.items():
            result[f"{prefix}{name}"] = obj
        for name, obj in self._agents.items():
            result[f"{prefix}{name}"] = obj
        for name, obj in self._functions.items():
            result[f"{prefix}{name}"] = obj
        for name, obj in self._channels.items():
            result[f"{prefix}{name}"] = obj
        for name, obj in self._patterns.items():
            result[f"{prefix}{name}"] = obj
        for name, obj in self._schedules.items():
            result[f"{prefix}{name}"] = obj

        for ns, sub in self._submodules.items():
            sub_prefix = f"{prefix}{ns}." if ns else prefix
            for qname, obj in sub._collect_all().items():
                # Strip sub's own prefix and re-apply ours
                bare = qname[len(sub.name) + 1 :] if qname.startswith(sub.name + ".") else qname

                # Only include exported symbols from submodules (respect encapsulation)
                sym_name = bare.split(".")[-1]
                if sym_name not in sub._exports and ns:
                    # Skip non-exported symbols when submodule is namespaced
                    continue

                result[f"{sub_prefix}{bare}"] = obj

        return result

    # ── Introspection ──────────────────────────────────────────────────────

    def describe(self) -> dict[str, Any]:
        """Return a structured description of all registered resources."""
        return {
            "name": self.name,
            "storage": list(self._storage.keys()),
            "agents": list(self._agents.keys()),
            "functions": list(self._functions.keys()),
            "channels": list(self._channels.keys()),
            "patterns": list(self._patterns.keys()),
            "schedules": list(self._schedules.keys()),
            "components": list(self._components.keys()),
            "submodules": {k: v.describe() for k, v in self._submodules.items()},
            "exports": list(self._exports),
        }

    def __repr__(self) -> str:
        return (
            f"Module({self.name!r}, "
            f"storage={list(self._storage)}, "
            f"agents={list(self._agents)}, "
            f"functions={list(self._functions)})"
        )
