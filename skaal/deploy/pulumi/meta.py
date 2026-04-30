from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, cast

from skaal.types import DeployMeta, TargetName

META_FILE = "skaal-meta.json"


def write_meta(
    output_dir: Path,
    target: TargetName,
    source_module: str,
    app_name: str,
    extra_fields: Mapping[str, Any] | None = None,
) -> Path:
    meta: dict[str, Any] = {
        "target": target,
        "source_module": source_module,
        "app_name": app_name,
    }
    if extra_fields:
        meta.update(extra_fields)
    path = output_dir / META_FILE
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return path


def read_meta(artifacts_dir: Path) -> DeployMeta:
    path = artifacts_dir / META_FILE
    if not path.exists():
        raise FileNotFoundError(
            f"{META_FILE} not found in {artifacts_dir}. Re-run `skaal build` to regenerate the artifacts."
        )
    return cast(DeployMeta, json.loads(path.read_text(encoding="utf-8")))
