from __future__ import annotations

from skaal.deploy.kinds import StorageKind
from skaal.deploy.plugin import BackendPlugin, Wiring

plugin = BackendPlugin(
    name="dynamodb",
    kinds=frozenset({StorageKind.KV}),
    wiring=Wiring(
        class_name="DynamoBackend",
        module="dynamodb_backend",
        env_prefix="SKAAL_TABLE",
    ),
    supported_targets=frozenset({"aws"}),
    local_fallbacks={StorageKind.KV: "local-map"},
)
