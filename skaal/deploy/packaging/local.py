from __future__ import annotations

from pathlib import Path

from skaal.deploy.packaging.docker_builder import build_image


def build_local_image(artifacts_dir: Path, image_name: str) -> str:
    print(f"==> Docker build context: {artifacts_dir.resolve()}", flush=True)
    return build_image(
        context_dir=artifacts_dir.resolve(),
        tag=image_name,
    )
