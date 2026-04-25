from __future__ import annotations

from skaal.deploy.kinds import StorageKind
from skaal.deploy.plugin import BackendPlugin, Wiring

plugin = BackendPlugin(
    name="chroma-local",
    kinds=frozenset({StorageKind.VECTOR}),
    wiring=Wiring(
        class_name="ChromaVectorBackend",
        module="chroma_backend",
        path_default="/app/data/chroma",
        uses_namespace=True,
        dependency_sets=("chroma-runtime",),
    ),
    supported_targets=frozenset({"local"}),
)
