"""Tests for skaal.errors helpers added in ADR 021."""

from __future__ import annotations

import pytest

from skaal.errors import (
    CatalogError,
    MissingExtraError,
    SkaalConfigError,
    SkaalError,
    SkaalSolverError,
    UnsatisfiableConstraints,
    require_extra,
)


def test_skaal_config_error_resolves():
    """The pre-ADR-021 NameError on import is gone."""
    assert issubclass(SkaalConfigError, SkaalError)
    assert issubclass(CatalogError, SkaalConfigError)


def test_unsatisfiable_constraints_inherits_from_solver_error():
    assert issubclass(UnsatisfiableConstraints, SkaalSolverError)
    assert issubclass(SkaalSolverError, SkaalError)


def test_unsatisfiable_constraints_carries_diagnosis_kwarg():
    exc = UnsatisfiableConstraints("X", "no backend", diagnosis="fake")
    assert exc.diagnosis == "fake"
    assert exc.exit_code == 2


def test_require_extra_passes_through_when_module_present():
    @require_extra("dummy", ["sys"])
    def f(x: int) -> int:
        return x + 1

    assert f(1) == 2


def test_require_extra_raises_missing_extra_error():
    @require_extra("frobnicator", ["nonexistent_module_xyz"], feature="frob")
    def f() -> None:
        return None

    with pytest.raises(MissingExtraError) as exc:
        f()
    msg = str(exc.value)
    assert "frob" in msg
    assert "skaal[frobnicator]" in msg
