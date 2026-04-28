"""DeployTarget Protocol — the formal interface for all deploy targets.

Every deploy target (AWS Lambda, GCP Cloud Run, local Docker + Pulumi, and any
future targets) must satisfy this Protocol.  The concrete adapters live in
:mod:`skaal.deploy.registry`; the generator modules (:mod:`skaal.deploy.aws`,
:mod:`skaal.deploy.gcp`, :mod:`skaal.deploy.local`) are the implementation
backends wrapped by those adapters.

Consistent with the :class:`~skaal.backends.base.StorageBackend` Protocol in
``backends/base.py`` — ``@runtime_checkable`` so that ``isinstance`` checks
work in tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from skaal.plan import PlanFile


@runtime_checkable
class DeployTarget(Protocol):
    """Protocol that every deploy target adapter must satisfy.

    Concrete implementations live in :mod:`skaal.deploy.registry`:
    :class:`~skaal.deploy.registry.AWSLambdaTarget`,
    :class:`~skaal.deploy.registry.GCPCloudRunTarget`,
    :class:`~skaal.deploy.registry.LocalDockerTarget`.

    Adding a new target
    -------------------
    1. Implement this protocol in a new class in ``registry.py``.
    2. Add the class (and any aliases) to ``_TARGET_REGISTRY``.
    That is all — no changes needed in ``build_cmd.py`` or ``push.py``.
    """

    name: str
    """Canonical target name, e.g. ``"aws"``, ``"gcp"``, ``"local"``."""

    default_region: str
    """Default cloud region, e.g. ``"us-east-1"``.
    Empty string for targets that do not use a cloud region (local)."""

    def generate_artifacts(
        self,
        app: Any,
        plan: "PlanFile",
        output_dir: Path,
        source_module: str,
        app_var: str = "app",
        *,
        region: str | None = None,
        dev: bool = False,
        stack_profile: dict[str, Any] | None = None,
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
        config_overrides: dict[str, str] | None = None,
    ) -> dict[str, str]:
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
