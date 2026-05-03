"""Thin loader for the canonical FastAPI file upload example."""

from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_MODULE_PATH = Path(__file__).with_name("07_file_upload_api") / "app.py"
_SPEC = spec_from_file_location("examples._file_upload_api_impl", _MODULE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Could not load example module from {_MODULE_PATH}")

_MODULE = module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

app = _MODULE.app
api = _MODULE.api

__all__ = ["app", "api"]
