"""Back-compat re-export of :mod:`skaal.settings`.

``SkaalSettings`` and its helpers used to live here but are now defined at
``skaal.settings`` so they can be shared between the CLI and the
:mod:`skaal.api` Python API.  This module re-exports them for any code still
importing from ``skaal.cli.config``.
"""

from __future__ import annotations

from skaal.settings import (
    PyprojectTomlSource,
    SkaalSettings,
    find_pyproject,
    load_skaal_section,
)

__all__ = [
    "PyprojectTomlSource",
    "SkaalSettings",
    "find_pyproject",
    "load_skaal_section",
]
