from __future__ import annotations

from skaal.deploy.kinds import StorageKind
from skaal.deploy.plugin import BackendPlugin, Wiring

plugin = BackendPlugin(
    name="memorystore-redis",
    kinds=frozenset({StorageKind.KV}),
    wiring=Wiring(
        class_name="RedisBackend",
        module="redis_backend",
        env_prefix="SKAAL_REDIS_URL",
        uses_namespace=True,
        requires_vpc=True,
        local_service="redis",
        local_env_value="redis://redis:6379",
    ),
    supported_targets=frozenset({"gcp"}),
    local_fallbacks={StorageKind.KV: "local-redis"},
)
