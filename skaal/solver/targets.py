"""Target family resolution — single source of truth for deploy target semantics.

All solver modules import from here instead of hardcoding target name strings.
This eliminates scattered ``target in ("aws-lambda", "aws")`` guards and makes
adding a new target a one-line change.

Usage::

    from skaal.solver.targets import TargetFamily, catalog_compute_key, is_serverless, resolve_family

    family = resolve_family("aws-lambda")   # TargetFamily.AWS
    is_serverless("gcp-cloudrun")           # True
    catalog_compute_key("aws")              # "lambda"
    catalog_compute_key("k8s")              # None
"""

from __future__ import annotations

from enum import Enum
from typing import Final


class TargetFamily(str, Enum):
    """Canonical compute families understood by the solver.

    Every accepted target string (canonical name *or* legacy alias) maps to
    exactly one ``TargetFamily``.  Downstream solver logic should branch on
    these values, not on raw target strings.
    """

    AWS = "aws"
    GCP = "gcp"
    LOCAL = "local"
    GENERIC = "generic"


# Maps every accepted target string → its canonical TargetFamily.
# Add new targets or aliases here; no other file needs to change.
_TARGET_FAMILIES: Final[dict[str, TargetFamily]] = {
    # ── AWS ───────────────────────────────────────────────────────────────────
    "aws": TargetFamily.AWS,
    "aws-lambda": TargetFamily.AWS,  # legacy alias
    # ── GCP ───────────────────────────────────────────────────────────────────
    "gcp": TargetFamily.GCP,
    "gcp-cloudrun": TargetFamily.GCP,  # legacy alias
    # ── Local ─────────────────────────────────────────────────────────────────
    "local": TargetFamily.LOCAL,
    "local-docker": TargetFamily.LOCAL,
    # ── Generic container / VM targets ────────────────────────────────────────
    "k8s": TargetFamily.GENERIC,
    "ecs": TargetFamily.GENERIC,
    "generic": TargetFamily.GENERIC,
}

# Families where VPC-attached backends incur an operational penalty.
# In serverless runtimes (Lambda, Cloud Run) adding a VPC connector introduces
# cold-start latency and extra configuration, so the solver de-prefers them.
_SERVERLESS_FAMILIES: Final[frozenset[TargetFamily]] = frozenset(
    {TargetFamily.AWS, TargetFamily.GCP}
)

# Maps a serverless TargetFamily → the catalog compute key that holds its
# deploy configuration (e.g. Lambda timeout, Cloud Run concurrency).
_CATALOG_COMPUTE_KEY: Final[dict[TargetFamily, str]] = {
    TargetFamily.AWS: "lambda",
    TargetFamily.GCP: "cloud-run",
}


def resolve_family(target: str) -> TargetFamily:
    """Map *target* (canonical name or alias) to its :class:`TargetFamily`.

    Unknown strings fall back to :attr:`TargetFamily.GENERIC` so that
    experimental or future targets degrade gracefully.
    """
    return _TARGET_FAMILIES.get(target, TargetFamily.GENERIC)


def is_serverless(target: str) -> bool:
    """Return ``True`` if *target* maps to a serverless execution family.

    Serverless families (AWS Lambda, GCP Cloud Run) have no persistent compute
    and incur a VPC connectivity penalty when attaching managed services.
    """
    return resolve_family(target) in _SERVERLESS_FAMILIES


def catalog_compute_key(target: str) -> str | None:
    """Return the catalog ``[compute.*]`` key for *target*'s serverless runtime.

    Returns ``None`` for non-serverless targets (k8s, ecs, generic) where the
    Z3 solver selects an instance type from the catalog instead.

    Examples::

        catalog_compute_key("aws")          # "lambda"
        catalog_compute_key("gcp-cloudrun") # "cloud-run"
        catalog_compute_key("k8s")          # None
    """
    return _CATALOG_COMPUTE_KEY.get(resolve_family(target))
