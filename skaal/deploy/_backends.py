"""Backend wiring helpers for deploy artifact generators.

``BackendHandler`` is a Pydantic model populated from the ``[storage.X.wire]``
section of a catalog TOML.  It is stored in ``StorageSpec.wire_params`` when
``skaal plan`` runs, so ``skaal build`` reads everything it needs from the plan
file — no catalog access at build time.

``import_stmt`` is derived automatically from ``module`` + ``class_name``,
so catalog wire sections stay minimal.

The only Python-side constants are:
- ``_LOCAL_SERVICE_SPECS`` — verbose local Docker service specs (stable, not
    user-facing config, not worth putting in TOML).
- ``_LOCAL_FALLBACK`` — maps cloud backend names to their local equivalents
  when a cloud plan is run with a local generator (edge case).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, computed_field

from skaal.types.deploy import LocalServiceSpec

if TYPE_CHECKING:
    from skaal.plan import StorageSpec


# ── BackendHandler ────────────────────────────────────────────────────────────


class BackendHandler(BaseModel):
    """Wiring metadata for a storage backend.

    Populated from ``[storage.X.wire]`` in a catalog TOML and stored in
    ``StorageSpec.wire_params``.  ``import_stmt`` is derived — never written
    to config.
    """

    model_config = ConfigDict(frozen=True)

    class_name: str
    """Python class name, e.g. ``"FirestoreBackend"``."""

    module: str
    """Module inside ``skaal.backends``, e.g. ``"firestore_backend"``.
    The full import is derived as
    ``from skaal.backends.{module} import {class_name}``."""

    env_prefix: str | None = None
    """Env var prefix, e.g. ``"SKAAL_COLLECTION"``.
    Full var: ``{env_prefix}_{CLASS_NAME_UPPER}``.
    ``None`` for backends that need no connection string (e.g. in-memory)."""

    path_default: str | None = None
    """Hardcoded first positional argument for file-based backends (e.g. SQLite).
    Used when ``env_prefix`` is ``None`` but the constructor needs a path.
    Example: ``"skaal_local.db"`` → ``SqliteBackend("skaal_local.db", namespace=…)``."""

    uses_namespace: bool = False
    """Constructor takes ``namespace=class_name`` as a keyword arg."""

    requires_vpc: bool = False
    """GCP: backend requires a VPC connector (Cloud SQL, Memorystore)."""

    local_service: str | None = None
    """Local service resource name this backend needs (``"redis"``,
    ``"postgres"``). ``None`` if no separate container is required."""

    local_env_value: str | None = None
    """Static DSN / URL injected into the local app container environment,
    e.g. ``"redis://redis:6379"``."""

    extra_deps: list[str] = Field(default_factory=list)
    """PyPI packages added to the artifact ``pyproject.toml``."""

    @computed_field  # type: ignore[misc]
    @property
    def import_stmt(self) -> str:
        """Full import statement derived from ``module`` + ``class_name``."""
        return f"from skaal.backends.{self.module} import {self.class_name}"


# ── Local Docker service specs ────────────────────────────────────────────────
# Kept in Python: verbose, stable, not user-facing config.
# Referenced by BackendHandler.local_service (service name key).

_LOCAL_SERVICE_SPECS: dict[str, LocalServiceSpec] = {
    "postgres": {
        "image": "postgres:16-alpine",
        "ports": [{"internal": 5432, "external": 5432}],
        "envs": [
            "POSTGRES_USER=skaal_user",
            "POSTGRES_PASSWORD=skaal_pass",
            "POSTGRES_DB=skaal_db",
        ],
        "healthcheck": {
            "tests": ["CMD-SHELL", "pg_isready -U skaal_user -d skaal_db"],
            "interval": "5s",
            "timeout": "5s",
            "retries": 5,
            "startPeriod": "10s",
        },
    },
    "redis": {
        "image": "redis:7-alpine",
        "ports": [{"internal": 6379, "external": 6379}],
        "envs": [],
        "healthcheck": {
            "tests": ["CMD", "redis-cli", "ping"],
            "interval": "5s",
            "timeout": "3s",
            "retries": 5,
            "startPeriod": "10s",
        },
    },
    # ── Proxy / API-gateway local backends ───────────────────────────────────
    "traefik": {
        "image": "traefik:v3",
        "ports": [{"internal": 80, "external": 80}],
        "envs": [],
        "volumes": [
            {
                "containerPath": "/var/run/docker.sock",
                "hostPath": "/var/run/docker.sock",
                "readOnly": True,
            }
        ],
        "command": [
            "--providers.docker=true",
            "--providers.docker.exposedbydefault=false",
            "--entrypoints.web.address=:80",
        ],
        "healthcheck": {
            "tests": ["CMD", "traefik", "healthcheck"],
            "interval": "10s",
            "timeout": "5s",
            "retries": 5,
            "startPeriod": "5s",
        },
    },
    "kong": {
        "image": "kong:3",
        "ports": [{"internal": 8000, "external": 8080}],
        "envs": [
            "KONG_DATABASE=off",
            "KONG_DECLARATIVE_CONFIG=/kong/config.yml",
            "KONG_PROXY_LISTEN=0.0.0.0:8000",
            "KONG_ADMIN_LISTEN=127.0.0.1:8001",
        ],
        "healthcheck": {
            "tests": ["CMD", "kong", "health"],
            "interval": "10s",
            "timeout": "5s",
            "retries": 5,
            "startPeriod": "15s",
        },
    },
}

# Maps cloud backend names to their local Docker equivalents.
# Only used when a cloud plan is built with the local generator (edge case).
_LOCAL_FALLBACK: dict[tuple[str, str], str] = {
    ("dynamodb", "kv"): "local-map",
    ("firestore", "kv"): "local-map",
    ("cloud-sql-postgres", "kv"): "local-redis",
    ("cloud-sql-postgres", "relational"): "sqlite",
    ("cloud-sql-pgvector", "vector"): "chroma-local",
    ("memorystore-redis", "kv"): "local-redis",
    ("rds-pgvector", "vector"): "chroma-local",
}


# ── Public accessor ───────────────────────────────────────────────────────────


def get_handler(spec: "StorageSpec", *, local: bool = False) -> BackendHandler:
    """Return the :class:`BackendHandler` for *spec*.

    Wire metadata is read from ``spec.wire_params``, which was populated by
    ``skaal plan`` from the ``[storage.<backend>.wire]`` catalog section.

    Args:
        spec:  A :class:`~skaal.plan.StorageSpec` from the plan file.
        local: If True, cloud backends are replaced with their local equivalents
               via ``_LOCAL_FALLBACK`` before returning, even when ``wire_params``
               is present. This prevents cloud-specific services (e.g. a postgres
               container) from appearing in a local Docker stack generated from
               a GCP or AWS plan.

    Raises:
        KeyError: If the spec has no wire metadata and no fallback exists.
    """
    if local:
        # Always apply the local fallback for cloud backends so that a plan
        # solved for GCP/AWS doesn't leak cloud-specific wire params (and their
        # local_service sidecars) into the local Docker stack.
        fallback_key = _LOCAL_FALLBACK.get((spec.backend, spec.kind))
        if fallback_key:
            _fallback = _FALLBACK_WIRE.get(fallback_key)
            if _fallback:
                return _fallback

    if spec.wire_params:
        return BackendHandler.model_validate(spec.wire_params)

    raise KeyError(
        f"Backend {spec.backend!r} has no [wire] section in the catalog. "
        "Add a [storage." + spec.backend + ".wire] entry to your catalog TOML."
    )


# Minimal fallback handlers for local builds of cloud plans.
# Covers the case where a GCP/AWS plan is run through the local generator.
_FALLBACK_WIRE: dict[str, BackendHandler] = {
    "local-map": BackendHandler(
        class_name="LocalMap",
        module="local_backend",
    ),
    "chroma-local": BackendHandler(
        class_name="ChromaVectorBackend",
        module="chroma_backend",
        path_default="/app/data/chroma",
        uses_namespace=True,
        extra_deps=["langchain-chroma>=1.1", "chromadb>=1.5"],
    ),
    "local-redis": BackendHandler(
        class_name="RedisBackend",
        module="redis_backend",
        env_prefix="SKAAL_REDIS_URL",
        uses_namespace=True,
        local_service="redis",
        local_env_value="redis://redis:6379",
        extra_deps=["redis>=5.0"],
    ),
}


# ── Wiring helpers ────────────────────────────────────────────────────────────


def _make_constructor(handler: BackendHandler, class_name: str, env_var: str) -> str:
    """Build the constructor call string for an entry-point template."""
    if handler.env_prefix is None:
        if handler.path_default and handler.uses_namespace:
            return f'{handler.class_name}("{handler.path_default}", namespace="{class_name}")'
        if handler.path_default:
            return f'{handler.class_name}("{handler.path_default}")'
        return f"{handler.class_name}()"
    if handler.uses_namespace:
        return f'{handler.class_name}(os.environ["{env_var}"], namespace="{class_name}")'
    return f'{handler.class_name}(os.environ["{env_var}"])'


def build_wiring(plan: Any, *, local: bool = False) -> tuple[str, str]:
    """Return ``(backend_imports, backend_overrides)`` for an entry-point template.

    Args:
        plan:  The solved :class:`~skaal.plan.PlanFile`.
        local: If True, resolve cloud backends to local equivalents.

    Returns:
        A 2-tuple:
        - *backend_imports*: one import line per unique backend class used.
        - *backend_overrides*: ``"ClassName": Backend(...)`` lines indented
          8 spaces, ready to embed in a dict literal.
    """
    seen: set[str] = set()
    import_lines: list[str] = []
    override_lines: list[str] = []

    for qname, spec in plan.storage.items():
        class_name = qname.split(".")[-1]
        handler = get_handler(spec, local=local)
        env_var = f"{handler.env_prefix}_{class_name.upper()}" if handler.env_prefix else ""

        if handler.import_stmt not in seen:
            seen.add(handler.import_stmt)
            import_lines.append(handler.import_stmt)

        ctor = _make_constructor(handler, class_name, env_var)
        override_lines.append(f'        "{class_name}": {ctor},')

    return "\n".join(import_lines), "\n".join(override_lines)


def build_wiring_aws(plan: Any) -> tuple[str, str]:
    """Return ``(backend_imports, backend_overrides)`` for an AWS Lambda entry point.

    AWS entry points use the backend selected in the plan for each storage
    class. Connection details are injected at runtime via the env var prefix
    declared in the backend's catalog ``[wire]`` section.
    """
    seen: set[str] = set()
    import_lines: list[str] = []
    override_lines: list[str] = []

    for qname, spec in plan.storage.items():
        class_name = qname.split(".")[-1]
        handler = get_handler(spec)
        env_var = f"{handler.env_prefix}_{class_name.upper()}" if handler.env_prefix else ""

        if handler.import_stmt not in seen:
            seen.add(handler.import_stmt)
            import_lines.append(handler.import_stmt)

        ctor = _make_constructor(handler, class_name, env_var)
        override_lines.append(f'        "{class_name}": {ctor},')

    return "\n".join(import_lines), "\n".join(override_lines)
