"""Skaal deploy module — generates and pushes deployment artifacts.

Public API:
- :func:`get_target` — look up a :class:`~skaal.deploy.target.DeployTarget`
  by name (e.g. ``"aws"``, ``"gcp"``, ``"local"``).
- :func:`package_and_push` — package and deploy artifacts produced by
  ``skaal build``.
"""

from skaal.deploy.push import package_and_push
from skaal.deploy.registry import get_target

__all__ = ["get_target", "package_and_push"]
