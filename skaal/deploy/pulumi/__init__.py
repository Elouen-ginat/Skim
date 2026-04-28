"""Thin subprocess wrapper around the ``pulumi`` CLI.

Nothing here knows about AWS, GCP, or any specific resource type.  It
just runs commands and wraps failures with stage-aware context.
"""

from skaal.deploy.pulumi.client import (
    DeployCommandError,
    PulumiClient,
    run_command,
)

__all__ = ["DeployCommandError", "PulumiClient", "run_command"]
