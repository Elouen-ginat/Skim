"""Backend wiring helpers for deploy artifact generators."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, computed_field

from skaal.deploy.backends.services import LOCAL_FALLBACK

if TYPE_CHECKING:
    from skaal.plan import StorageSpec


class BackendHandler(BaseModel):
    model_config = ConfigDict(frozen=True)

    class_name: str
    module: str
    env_prefix: str | None = None
    path_default: str | None = None
    uses_namespace: bool = False
    requires_vpc: bool = False
    local_service: str | None = None
    local_env_value: str | None = None
    extra_deps: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[misc]
    @property
    def import_stmt(self) -> str:
        return f"from skaal.backends.{self.module} import {self.class_name}"


FALLBACK_WIRE: dict[str, BackendHandler] = {
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


def get_handler(spec: "StorageSpec", *, local: bool = False) -> BackendHandler:
    if local:
        fallback_key = LOCAL_FALLBACK.get((spec.backend, spec.kind))
        if fallback_key:
            fallback = FALLBACK_WIRE.get(fallback_key)
            if fallback is not None:
                return fallback

    if spec.wire_params:
        return BackendHandler.model_validate(spec.wire_params)

    raise KeyError(
        f"Backend {spec.backend!r} has no [wire] section in the catalog. "
        f"Add a [storage.{spec.backend}.wire] entry to your catalog TOML."
    )
