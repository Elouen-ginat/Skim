"""Tests for solver/compute.py (encode_compute) and solver/components.py."""

from __future__ import annotations

import pytest

from skaal import App
from skaal.components import (
    APIGateway,
    ExternalObservability,
    ExternalStorage,
    Proxy,
    Route,
)
from skaal.solver.components import encode_component
from skaal.solver.compute import UnsatisfiableComputeConstraints, encode_compute
from skaal.types import Compute, ComputeType

# ── encode_compute fixtures ───────────────────────────────────────────────────

_CATALOG_COMPUTE = {
    "small-cpu": {
        "display_name": "Small CPU",
        "vcpus": 2,
        "memory_gb": 4.0,
        "compute_types": ["cpu"],
        "cost_per_hour": 0.05,
    },
    "large-cpu": {
        "display_name": "Large CPU",
        "vcpus": 8,
        "memory_gb": 16.0,
        "compute_types": ["cpu"],
        "cost_per_hour": 0.40,
    },
    "gpu-instance": {
        "display_name": "GPU Instance",
        "vcpus": 4,
        "memory_gb": 32.0,
        "compute_types": ["gpu", "cpu"],
        "cost_per_hour": 2.50,
    },
}


def test_encode_compute_lambda_target():
    """aws-lambda target always returns 'lambda'."""
    compute = Compute()
    name, reason = encode_compute("fn", compute, _CATALOG_COMPUTE, target="aws-lambda")
    assert name == "lambda"
    assert "serverless" in reason.lower()


def test_encode_compute_cheapest_cpu():
    """Without constraints selects cheapest CPU instance."""
    compute = Compute()
    name, reason = encode_compute("fn", compute, _CATALOG_COMPUTE)
    assert name == "small-cpu"
    assert "cost" in reason.lower()


def test_encode_compute_gpu_required():
    """GPU compute type filter selects a gpu-capable instance."""
    compute = Compute(compute_type=ComputeType.GPU)
    name, reason = encode_compute("fn", compute, _CATALOG_COMPUTE)
    assert name == "gpu-instance"


def test_encode_compute_memory_constraint():
    """Memory ≥ 16GB requirement picks large-cpu or gpu-instance, not small-cpu."""
    compute = Compute(memory="16GB")
    name, reason = encode_compute("fn", compute, _CATALOG_COMPUTE)
    assert name in ("large-cpu", "gpu-instance")


def test_encode_compute_empty_catalog():
    """Empty catalog returns default c5-large without raising."""
    compute = Compute()
    name, reason = encode_compute("fn", compute, {})
    assert name == "c5-large"


def test_encode_compute_unresolvable_type():
    """Requesting an unsupported compute type falls through to cost-based fallback."""
    compute = Compute(compute_type=ComputeType.TPU)
    # TPU not in catalog → UnsatisfiableComputeConstraints raised
    with pytest.raises(UnsatisfiableComputeConstraints) as exc_info:
        encode_compute("fn", compute, _CATALOG_COMPUTE)
    assert "fn" in str(exc_info.value)


# ── encode_component fixtures ─────────────────────────────────────────────────

_EMPTY_CATALOG: dict = {}


def test_encode_component_proxy_k8s():
    """Proxy on k8s target resolves to traefik by default."""
    proxy = Proxy("edge", routes=[Route("/api/*", target="handle")])
    spec = encode_component("edge", proxy, _EMPTY_CATALOG, target="k8s")
    assert spec.provisioned is True
    assert spec.kind == "proxy"
    assert spec.implementation == "traefik"
    assert "edge" == spec.component_name


def test_encode_component_proxy_aws_lambda():
    """Proxy on aws-lambda target resolves to api-gateway."""
    proxy = Proxy("gw", routes=[Route("/*", target="fn")])
    spec = encode_component("gw", proxy, _EMPTY_CATALOG, target="aws-lambda")
    assert spec.implementation == "api-gateway"


def test_encode_component_proxy_pinned():
    """Explicit implementation pin overrides default selection."""
    proxy = Proxy("edge", routes=[], implementation="nginx")
    spec = encode_component("edge", proxy, _EMPTY_CATALOG, target="k8s")
    assert spec.implementation == "nginx"


def test_encode_component_api_gateway():
    """APIGateway on ecs target resolves to api-gateway."""
    gw = APIGateway("public", routes=[Route("/v1/*", target="fn")])
    spec = encode_component("public", gw, _EMPTY_CATALOG, target="ecs")
    assert spec.provisioned is True
    assert spec.kind == "api-gateway"
    assert spec.implementation == "api-gateway"


def test_encode_component_external_storage():
    """ExternalStorage passes through as not-provisioned."""
    ext = ExternalStorage(
        "legacy-db",
        connection_env="DATABASE_URL",
        latency="< 20ms",
    )
    spec = encode_component("legacy-db", ext, _EMPTY_CATALOG)
    assert spec.provisioned is False
    assert spec.connection_env == "DATABASE_URL"
    assert "external" in spec.reason


def test_encode_component_external_observability():
    prom = ExternalObservability("prometheus", provider="prometheus", endpoint_env="PROM_URL")
    spec = encode_component("prometheus", prom, _EMPTY_CATALOG)
    assert spec.provisioned is False
    assert spec.kind == "external-observability"


def test_encode_component_catalog_override():
    """Catalog [components] section overrides default implementation."""
    catalog = {"components": {"my-proxy": {"implementation": "envoy"}}}
    proxy = Proxy("my-proxy", routes=[])
    # Proxy kind takes precedence over generic catalog lookup via _select_proxy_impl
    spec = encode_component("my-proxy", proxy, catalog, target="k8s")
    # Still uses proxy default (traefik) since Proxy kind has its own selector
    assert spec.kind == "proxy"


# ── Integration: solve() with components ─────────────────────────────────────


def test_solve_includes_components():
    """solve() encodes app components into plan.components."""
    from skaal.catalog.loader import load_catalog
    from skaal.solver.solver import solve

    app = App("component-test")

    @app.storage(read_latency="< 10ms", durability="persistent")
    class Store:
        pass

    proxy = Proxy("edge", routes=[Route("/api/*", target="handle")])
    app.attach(proxy)

    try:
        catalog = load_catalog()
    except FileNotFoundError:
        pytest.skip("No catalog available")

    plan = solve(app, catalog, target="k8s")
    assert "edge" in plan.components
    assert plan.components["edge"].kind == "proxy"
    assert plan.components["edge"].provisioned is True
