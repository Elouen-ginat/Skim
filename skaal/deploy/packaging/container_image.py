from __future__ import annotations

from pathlib import Path

from skaal.deploy.push import _build_push_image


class ContainerImagePackager:
    def publish(
        self,
        artifacts_dir: Path,
        *,
        project: str,
        region: str,
        repository: str,
        image_name: str,
    ) -> str:
        _build_push_image(artifacts_dir, project, region, repository, image_name)
        return f"{region}-docker.pkg.dev/{project}/{repository}/{image_name}:latest"
