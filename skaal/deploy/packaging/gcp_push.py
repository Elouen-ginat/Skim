from __future__ import annotations

from pathlib import Path

import google.auth
from google.auth.transport.requests import Request

from skaal.deploy.packaging.docker_builder import build_image, login_registry, push_image


def build_and_push_image(
    artifacts_dir: Path,
    project: str,
    region: str,
    repository: str,
    app_name: str,
) -> None:
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    credentials.refresh(Request())
    if credentials.token is None:
        raise RuntimeError("Google credentials did not yield an access token.")

    registry = f"{region}-docker.pkg.dev"
    image_repository = f"{registry}/{project}/{repository}/{app_name}"
    image_tag = f"{image_repository}:latest"

    login_registry(
        registry=registry,
        username="oauth2accesstoken",
        password=credentials.token,
    )
    build_image(context_dir=artifacts_dir.resolve(), tag=image_tag)
    push_image(repository=image_repository, tag="latest")
