"""skaal.runtime.local — local development runtime.

Re-exports :class:`~skaal.local.runtime.LocalRuntime` from ``skaal.local``
so the canonical import path ``from skaal.runtime.local import LocalRuntime``
works while ``skaal.local.runtime`` remains importable for backward compat.
"""

from skaal.local.runtime import LocalRuntime  # noqa: F401

__all__ = ["LocalRuntime"]
