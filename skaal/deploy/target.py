"""DeployTarget Protocol — the formal interface for all deploy targets.

Every deploy target must satisfy this Protocol. Concrete implementations are
assembled in :mod:`skaal.deploy.targets.registry`, while target-specific
artifact generation lives under :mod:`skaal.deploy.targets`.

Consistent with the :class:`~skaal.backends.base.StorageBackend` Protocol in
``backends/base.py`` — ``@runtime_checkable`` so that ``isinstance`` checks work
in tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from skaal.types import AppLike, ConfigOverrides, StackOutputs, StackProfile

if TYPE_CHECKING:
    from skaal.plan import PlanFile


@runtime_checkable
class DeployTarget(Protocol):
    """Protocol that every deploy target adapter must satisfy.

    Concrete implementations live in the deploy target registry.

    Adding a new target
    -------------------
    1. Assemble a target strategy in ``targets/registry.py``.
    2. Register it under the desired names.
    """

    name: str
    """Canonical target name, e.g. ``"aws"``, ``"gcp"``, ``"local"``."""

    default_region: str
    """Default cloud region, e.g. ``"us-east-1"``.
    Empty string for targets that do not use a cloud region (local)."""

    def generate_artifacts(
        self,
        app: AppLike,
        plan: "PlanFile",
        output_dir: Path,
        source_module: str,
        app_var: str = "app",
        *,
        region: str | None = None,
        dev: bool = False,
        stack_profile: StackProfile | None = None,
    ) -> list[Path]:
        """Generate all deployment artifacts into *output_dir*.

        Args:
            app:           Skaal :class:`~skaal.app.App` instance.
            plan:          Solved :class:`~skaal.plan.PlanFile`.
            output_dir:    Directory to write files into (created if absent).
            source_module: Dotted Python module path, e.g. ``"examples.counter"``.
            app_var:       Variable name of the App in the module (default ``"app"``).
            region:        Cloud region override.  Falls back to
                           :attr:`default_region` when None.
            dev:           Bundle local skaal source into the artifact
                           (local target only).
            stack_profile: Optional dict of stack-specific knobs baked into
                           the Pulumi artifact.  Recognised keys include
                           ``env`` (``dict[str, str]``), ``invokers``
                           (``list[str]``), and ``labels`` (``dict[str, str]``).
                           Unknown keys are ignored so targets can add more
                           over time without breaking callers.

        Returns:
            List of generated :class:`~pathlib.Path` objects.
        """
        ...

    def package_and_push(
        self,
        artifacts_dir: Path,
        *,
        stack: str,
        region: str | None,
        gcp_project: str | None,
        yes: bool,
        project_root: Path,
        source_module: str,
        app_name: str,
        config_overrides: ConfigOverrides | None = None,
    ) -> StackOutputs:
        """Package and deploy the artifacts; return Pulumi stack outputs.

        Args:
            artifacts_dir:    Path to the directory produced by
                              :meth:`generate_artifacts`.
            stack:            Pulumi stack name (default: ``"dev"``).
            region:           Cloud region override.
            gcp_project:      GCP project ID (required for GCP target).
            yes:              Pass ``--yes`` to ``pulumi up`` (non-interactive).
            project_root:     Root of the user's project (parent of artifacts_dir).
            source_module:    Dotted Python module path of the app.
            app_name:         App name (from ``skaal-meta.json``).
            config_overrides: Extra ``pulumi config set`` key/value pairs
                              applied after the core project/region config,
                              e.g. ``{"cloudRunMemory": "1Gi"}``.

        Returns:
            Dict of Pulumi stack outputs, e.g. ``{"apiUrl": "https://..."}``.
            Empty dict for targets that do not produce outputs (local).
        """
        ...

    def destroy_stack(
        self,
        artifacts_dir: Path,
        *,
        stack: str,
        yes: bool,
    ) -> None:
        """Destroy resources for an existing Pulumi stack.

        Args:
            artifacts_dir: Path to the directory produced by
                           :meth:`generate_artifacts`.
            stack:         Pulumi stack name.
            yes:           Pass ``--yes`` to ``pulumi destroy``.
        """
        ...
