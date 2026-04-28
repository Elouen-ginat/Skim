from __future__ import annotations

from skaal.backends._registry import get_backend_impl
from skaal.backends._spec import BackendSpec, Wiring, resolve_wiring
from skaal.backends.kv.local_map import LocalMap
from skaal.backends.kv.sqlite import SqliteBackend
from skaal.deploy.runtime_assets import collect_runtime_dependencies
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


def test_resolve_wiring_normalizes_legacy_extra_deps_into_dependencies() -> None:
    plugin = BackendSpec(
        name="local-redis",
        kinds=frozenset({"kv"}),
        wiring=Wiring(class_name="RedisBackend", module="kv.redis"),
    )
    spec = StorageSpec(
        variable_name="demo.Counter",
        backend="local-redis",
        wire_params={"extra_deps": ["redis>=5.0"]},
    )

    wiring = resolve_wiring(plugin, spec)

    assert spec.wire_params == {"dependencies": ["redis>=5.0"]}
    assert wiring.dependencies == ("redis>=5.0",)
    assert wiring.dependency_sets == ()


def test_collect_runtime_dependencies_includes_explicit_wiring_dependencies() -> None:
    plan = PlanFile(
        app_name="demo",
        storage={
            "demo.Counter": StorageSpec(
                variable_name="demo.Counter",
                backend="local-map",
                kind="kv",
                wire_params={"dependencies": ["redis>=5.0"]},
            )
        },
    )

    deps = collect_runtime_dependencies(
        plan,
        "does.not.exist",
        target="local",
        base_dependency_sets=[],
    )

    assert deps == ["redis>=5.0"]
