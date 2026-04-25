from __future__ import annotations

from skaal.deploy.kinds import StorageKind
from skaal.deploy.plugin import BackendPlugin, Wiring

plugin = BackendPlugin(
    name="local-map",
    kinds=frozenset({StorageKind.KV}),
    wiring=Wiring(
        class_name="SqliteBackend",
        module="sqlite_backend",
        path_default="/app/data/skaal_local.db",
        uses_namespace=True,
        dependency_sets=("sqlite-driver",),
    ),
    supported_targets=frozenset({"local"}),
)
