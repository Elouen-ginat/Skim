from __future__ import annotations

from skaal.deploy.kinds import StorageKind
from skaal.deploy.plugin import BackendPlugin, Wiring

plugin = BackendPlugin(
    name="sqlite",
    kinds=frozenset({StorageKind.KV, StorageKind.RELATIONAL}),
    wiring=Wiring(
        class_name="SqliteBackend",
        module="sqlite_backend",
        env_prefix="SKAAL_SQLITE_PATH",
        local_env_value="/app/data/skaal.db",
    ),
    supported_targets=frozenset({"local"}),
)
