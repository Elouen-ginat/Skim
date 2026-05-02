"""Did-you-mean hints on the constraint enums (ADR 021 §A.4)."""

from __future__ import annotations

import pytest

from skaal.types import AccessPattern, Consistency, Durability


def test_access_pattern_invalid_value_suggests_close_match():
    with pytest.raises(ValueError) as exc:
        AccessPattern("rand-read")
    assert "random-read" in str(exc.value)
    assert "Valid values:" in str(exc.value)


def test_durability_invalid_value_lists_alternatives():
    with pytest.raises(ValueError) as exc:
        Durability("nope")
    text = str(exc.value)
    for valid in ("ephemeral", "persistent", "durable"):
        assert valid in text


def test_consistency_invalid_value_suggests_close_match():
    with pytest.raises(ValueError) as exc:
        Consistency("eventua")
    assert "eventual" in str(exc.value)


def test_valid_values_still_resolve():
    """Existing happy-path lookups are unchanged."""
    assert Durability("ephemeral") is Durability.EPHEMERAL
    assert AccessPattern("random-read") is AccessPattern.RANDOM_READ
    assert Consistency("strong") is Consistency.STRONG
