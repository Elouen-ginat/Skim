from __future__ import annotations

from pathlib import Path

from skaal.deploy.push import _package_aws


class LambdaZipPackager:
    def package(self, artifacts_dir: Path, *, project_root: Path, source_module: str) -> None:
        _package_aws(artifacts_dir, project_root, source_module)
