from __future__ import annotations

from skaal.deploy.kinds import StorageKind
from skaal.deploy.plugin import BackendPlugin, Wiring

plugin = BackendPlugin(
    name="firestore",
    kinds=frozenset({StorageKind.KV}),
    wiring=Wiring(
        class_name="FirestoreBackend",
        module="firestore_backend",
        env_prefix="SKAAL_COLLECTION",
    ),
    supported_targets=frozenset({"gcp"}),
    local_fallbacks={StorageKind.KV: "local-map"},
)
