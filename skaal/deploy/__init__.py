"""Deploy registries and orchestration for Skaal artifact build / deploy."""

from skaal.deploy.pipeline import build_artifacts, deploy_artifacts
from skaal.deploy.plugin import BackendPlugin
from skaal.deploy.registry import (
    backend_registry,
    get_backend_plugin,
    get_target,
    register_backend,
    register_target,
    resolve_backend_plugin,
    target_registry,
)
from skaal.deploy.targets.base import Target

__all__ = [
    "BackendPlugin",
    "Target",
    "backend_registry",
    "build_artifacts",
    "deploy_artifacts",
    "get_backend_plugin",
    "get_target",
    "register_backend",
    "register_target",
    "resolve_backend_plugin",
    "target_registry",
]
