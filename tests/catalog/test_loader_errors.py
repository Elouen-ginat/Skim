"""Catalog-loader error wrapping (ADR 021 §A.4)."""

from __future__ import annotations

import pytest

from skaal.catalog.loader import load_catalog
from skaal.errors import CatalogError


def test_invalid_toml_raises_catalog_error_naming_path(tmp_path):
    bad = tmp_path / "broken.toml"
    bad.write_text("[storage.x\n", encoding="utf-8")  # missing closing bracket
    with pytest.raises(CatalogError) as exc:
        load_catalog(bad)
    msg = str(exc.value)
    assert str(bad) in msg
    assert "invalid TOML" in msg


def test_valid_toml_still_loads(tmp_path):
    ok = tmp_path / "ok.toml"
    ok.write_text('[storage.x]\ndisplay_name = "X"\n', encoding="utf-8")
    data = load_catalog(ok)
    assert data["storage"]["x"]["display_name"] == "X"
