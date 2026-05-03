from __future__ import annotations

from pathlib import Path

from skaal.deploy.packaging.docker_builder import DockerProgress, build_image


def build_local_image(
    artifacts_dir: Path,
    image_name: str,
    progress: DockerProgress = None,
) -> str:
    return build_image(
        context_dir=artifacts_dir.resolve(),
        tag=image_name,
        progress=progress,
    )
