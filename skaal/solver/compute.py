"""Compute constraint encoding for the Z3 solver."""

from __future__ import annotations

from typing import Any


def encode_compute(function_name: str, constraints: dict[str, Any], instance_types: list[dict[str, Any]]) -> Any:
    """
    Encode compute constraints into Z3 assertions and return the optimizer.

    Args:
        function_name:  The Skim function being solved (e.g. "predict_churn").
        constraints:    Parsed compute decorator metadata (__skim_compute__).
        instance_types: List of candidate instance types from the catalog.

    Returns:
        A z3.Optimize instance ready to be checked.
    """
    raise NotImplementedError("Compute constraint encoding (Phase 2).")
