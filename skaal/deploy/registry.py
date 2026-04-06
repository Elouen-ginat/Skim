"""Target adapter classes and the target registry.

Each class wraps one generator module (:mod:`~skaal.deploy.aws`,
:mod:`~skaal.deploy.gcp`, :mod:`~skaal.deploy.local`) and implements the
:class:`~skaal.deploy.target.DeployTarget` protocol.  Generator modules are
imported lazily (inside method bodies) to avoid circular imports at
module-load time.

The :data:`_TARGET_REGISTRY` maps every accepted target name (canonical and
alias) to the singleton adapter instance.  Use :func:`get_target` for safe
lookup with a helpful error message.

Adding a new target
-------------------
1. Implement :class:`~skaal.deploy.target.DeployTarget` in a new class here.
2. Add it (with all desired aliases) to :data:`_TARGET_REGISTRY`.
No changes are needed in ``build_cmd.py`` or ``push.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from skaal.deploy.target import DeployTarget

if TYPE_CHECKING:
    from skaal.plan import PlanFile


# ── AWS Lambda ────────────────────────────────────────────────────────────────


class AWSLambdaTarget:
    """Deploy target adapter for AWS Lambda + API Gateway + DynamoDB."""

    name = "aws"
    default_region = "us-east-1"

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
    ) -> list[Path]:
        from skaal.deploy.aws import generate_artifacts

        return generate_artifacts(
            app=app,
            plan=plan,
            output_dir=output_dir,
            source_module=source_module,
            app_var=app_var,
        )

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
    ) -> dict[str, str]:
        from skaal.deploy.push import (
            _package_aws,
            _pulumi_config_set,
            _pulumi_output,
            _pulumi_stack_select_or_init,
            _pulumi_up,
        )

        resolved_region = region or self.default_region
        _pulumi_stack_select_or_init(artifacts_dir, stack)
        _pulumi_config_set(artifacts_dir, {"aws:region": resolved_region})

        typer.echo("==> Packaging Lambda ...")
        _package_aws(artifacts_dir, project_root, source_module)

        typer.echo("==> Deploying (pulumi up) ...")
        _pulumi_up(artifacts_dir, yes=yes)

        api_url = _pulumi_output(artifacts_dir, "apiUrl")
        typer.echo(f"\nApp URL: {api_url}")
        return {"apiUrl": api_url}


# ── GCP Cloud Run ─────────────────────────────────────────────────────────────


class GCPCloudRunTarget:
    """Deploy target adapter for GCP Cloud Run."""

    name = "gcp"
    default_region = "us-central1"

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
    ) -> list[Path]:
        from skaal.deploy.gcp import generate_artifacts

        return generate_artifacts(
            app=app,
            plan=plan,
            output_dir=output_dir,
            source_module=source_module,
            app_var=app_var,
            region=region or self.default_region,
        )

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
    ) -> dict[str, str]:
        from skaal.deploy.push import (
            _build_push_image,
            _pulumi_config_set,
            _pulumi_output,
            _pulumi_stack_select_or_init,
            _pulumi_up,
        )

        if not gcp_project:
            raise ValueError(
                "GCP project is required for --target=gcp. "
                "Pass --gcp-project PROJECT or set SKAAL_GCP_PROJECT."
            )

        resolved_region = region or self.default_region
        _pulumi_stack_select_or_init(artifacts_dir, stack)
        _pulumi_config_set(
            artifacts_dir,
            {"gcp:project": gcp_project, "gcp:region": resolved_region},
        )

        typer.echo("==> Provisioning infrastructure (pulumi up) ...")
        _pulumi_up(artifacts_dir, yes=yes)

        repo = _pulumi_output(artifacts_dir, "imageRepository")
        typer.echo(f"==> Building and pushing image to {repo} ...")
        _build_push_image(artifacts_dir, gcp_project, resolved_region, repo, app_name)

        typer.echo("==> Deploying image to Cloud Run (pulumi up) ...")
        _pulumi_up(artifacts_dir, yes=yes)

        service_url = _pulumi_output(artifacts_dir, "serviceUrl")
        typer.echo(f"\nApp URL: {service_url}")
        return {"serviceUrl": service_url}


# ── Local Docker Compose ──────────────────────────────────────────────────────


class LocalDockerComposeTarget:
    """Deploy target adapter for local Docker Compose."""

    name = "local"
    default_region = ""

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
    ) -> list[Path]:
        from skaal.deploy.local import generate_artifacts

        return generate_artifacts(
            app=app,
            plan=plan,
            output_dir=output_dir,
            source_module=source_module,
            app_var=app_var,
            dev=dev,
        )

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
    ) -> dict[str, str]:
        from skaal.deploy.push import _run

        typer.echo("==> Starting local stack (docker compose up --build) ...")
        _run(["docker", "compose", "up", "--build"], cwd=artifacts_dir)
        return {}


# ── Registry ──────────────────────────────────────────────────────────────────

# Singleton adapter instances (stateless — safe to reuse).
_aws = AWSLambdaTarget()
_gcp = GCPCloudRunTarget()
_local = LocalDockerComposeTarget()

_TARGET_REGISTRY: dict[str, DeployTarget] = {
    # Canonical names
    "aws": _aws,
    "gcp": _gcp,
    "local": _local,
    # Aliases accepted by the solver / lock file / CLI
    "aws-lambda": _aws,
    "gcp-cloudrun": _gcp,
    "local-compose": _local,
}


def get_target(name: str) -> DeployTarget:
    """Return the :class:`~skaal.deploy.target.DeployTarget` for *name*.

    Args:
        name: Target name as it appears in ``plan.skaal.lock`` or the CLI,
              e.g. ``"aws"``, ``"gcp-cloudrun"``, ``"local"``.

    Raises:
        ValueError: If *name* is not in the registry, with the list of known
                    canonical target names.
    """
    try:
        return _TARGET_REGISTRY[name]
    except KeyError:
        known = sorted({t.name for t in _TARGET_REGISTRY.values()})
        raise ValueError(f"Unknown deploy target {name!r}. Supported targets: {known}") from None
