from __future__ import annotations

from skaal.backends._registry import get_backend_impl
from skaal.backends.local_backend import LocalMap
from skaal.backends.sqlite_backend import SqliteBackend
from skaal.deploy.wiring import build_runtime_wiring
from skaal.plan import PlanFile, StorageSpec


def test_build_runtime_wiring_local_map_uses_local_map() -> None:
    plan = PlanFile(
        app_name="demo",
        storage={
            "demo.Counter": StorageSpec(
                variable_name="demo.Counter",
                backend="local-map",
                kind="kv",
            )
        },
    )

    imports, overrides = build_runtime_wiring(plan, target="local")

    assert "from skaal.backends.kv.local_map import LocalMap" in imports
    assert '"Counter": LocalMap(),' in overrides


def test_backend_registry_returns_direct_impls() -> None:
    assert get_backend_impl("local-map") is LocalMap
    assert get_backend_impl("sqlite") is SqliteBackend
