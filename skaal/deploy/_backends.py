"""Backend wiring helpers for deploy artifact generators.

``BackendHandler`` is a Pydantic model populated from the ``[storage.X.wire]``
section of a catalog TOML.  It is stored in ``StorageSpec.wire_params`` when
``skaal plan`` runs, so ``skaal build`` reads everything it needs from the plan
file — no catalog access at build time.

``import_stmt`` is derived automatically from ``module`` + ``class_name``,
so catalog wire sections stay minimal.

The only Python-side constants are:
- ``_COMPOSE_SERVICES`` — verbose Docker Compose service specs (stable, not
  user-facing config, not worth putting in TOML).
- ``_LOCAL_FALLBACK`` — maps cloud backend names to their local equivalents
  when a cloud plan is run with a local generator (edge case).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, computed_field

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
    """Docker Compose service name this backend needs (``"redis"``,
    ``"postgres"``).  ``None`` if no separate container is required."""

    local_env_value: str | None = None
    """Static DSN / URL injected into the Compose ``environment`` block,
    e.g. ``"redis://redis:6379"``."""

    extra_deps: list[str] = Field(default_factory=list)
    """PyPI packages added to the artifact ``pyproject.toml``."""

    @computed_field  # type: ignore[misc]
    @property
    def import_stmt(self) -> str:
        """Full import statement derived from ``module`` + ``class_name``."""
        return f"from skaal.backends.{self.module} import {self.class_name}"


# ── Docker Compose service specs ──────────────────────────────────────────────
# Kept in Python: verbose, stable, not user-facing config.
# Referenced by BackendHandler.local_service (service name key).

_COMPOSE_SERVICES: dict[str, dict[str, Any]] = {
    "postgres": {
        "image": "postgres:16-alpine",
        "ports": ["- '5432:5432'"],
        "environment": [
            "- POSTGRES_USER=skaal_user",
            "- POSTGRES_PASSWORD=skaal_pass",
            "- POSTGRES_DB=skaal_db",
        ],
        "healthcheck": {
            "test": ["CMD-SHELL", "pg_isready -U skaal_user -d skaal_db"],
            "interval": "5s",
            "timeout": "5s",
            "retries": 5,
            "start_period": "10s",
        },
    },
    "redis": {
        "image": "redis:7-alpine",
        "ports": ["- '6379:6379'"],
        "environment": [],
        "healthcheck": {
            "test": ["CMD", "redis-cli", "ping"],
            "interval": "5s",
            "timeout": "3s",
            "retries": 5,
            "start_period": "10s",
        },
    },
    # ── Proxy / API-gateway local backends ───────────────────────────────────
    "traefik": {
        "image": "traefik:v3",
        "ports": ["- '80:80'"],
        "environment": [],
        "volumes": ["- /var/run/docker.sock:/var/run/docker.sock:ro"],
        "command": [
            "--providers.docker=true",
            "--providers.docker.exposedbydefault=false",
            "--entrypoints.web.address=:80",
        ],
        "healthcheck": {
            "test": ["CMD", "traefik", "healthcheck"],
            "interval": "10s",
            "timeout": "5s",
            "retries": 5,
            "start_period": "5s",
        },
    },
    "kong": {
        "image": "kong:3",
        "ports": ["- '8080:8000'"],
        "environment": [
            "- KONG_DATABASE=off",
            "- KONG_DECLARATIVE_CONFIG=/kong/config.yml",
            "- KONG_PROXY_LISTEN=0.0.0.0:8000",
            "- KONG_ADMIN_LISTEN=127.0.0.1:8001",
        ],
        "volumes": ["- ./kong.yml:/kong/config.yml:ro"],
        "healthcheck": {
            "test": ["CMD", "kong", "health"],
            "interval": "10s",
            "timeout": "5s",
            "retries": 5,
            "start_period": "15s",
        },
    },
}

# Maps cloud backend names to their local Docker Compose equivalents.
# Only used when a cloud plan is built with the local generator (edge case).
_LOCAL_FALLBACK: dict[str, str] = {
    "dynamodb": "local-map",
    "firestore": "local-map",
    "cloud-sql-postgres": "local-redis",
    "memorystore-redis": "local-redis",
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
               is present.  This prevents cloud-specific services (e.g. a postgres
               container) from appearing in a local docker-compose generated from
               a GCP or AWS plan.

    Raises:
        KeyError: If the spec has no wire metadata and no fallback exists.
    """
    if local:
        # Always apply the local fallback for cloud backends so that a plan
        # solved for GCP/AWS doesn't leak cloud-specific wire params (and their
        # local_service sidecars) into the local docker-compose.
        fallback_key = _LOCAL_FALLBACK.get(spec.backend)
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

    Lambda always maps every storage class to DynamoDB regardless of what the
    solver chose.  Table names are injected at runtime via
    ``SKAAL_TABLE_<CLASSNAME>`` env vars set by the Pulumi stack.
    """
    import_lines: list[str] = []
    override_lines: list[str] = []

    if plan.storage:
        # Use the wire data from the first spec — all will be dynamodb on AWS.
        first_spec = next(iter(plan.storage.values()))
        handler = get_handler(first_spec)
        import_lines.append(handler.import_stmt)

        for qname in plan.storage:
            class_name = qname.split(".")[-1]
            env_var = f"{handler.env_prefix}_{class_name.upper()}"
            override_lines.append(
                f'        "{class_name}": {handler.class_name}(os.environ["{env_var}"]),',
            )

    return "\n".join(import_lines), "\n".join(override_lines)
