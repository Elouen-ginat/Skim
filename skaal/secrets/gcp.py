"""GCP Secret Manager runtime resolver.

On Cloud Run, the deploy-time wiring uses ``env_from.secret_key_ref`` so
the value already lives in ``os.environ`` — :class:`~skaal.secrets._LazyGcp`
short-circuits to the env reader in that case.  This resolver covers the
local-dev path where the SDK call is necessary.
"""

from __future__ import annotations

import logging
import os

from skaal.errors import SecretMissingError, require_extra
from skaal.types.secret import ResolvedSecret, SecretProvider, SecretSpec

_LOG = logging.getLogger("skaal.secrets.gcp")


def _normalise_path(source: str) -> str:
    """Accept ``projects/<id>/secrets/<name>`` or just ``<name>``.

    When only the short name is provided, fall back to ``GOOGLE_CLOUD_PROJECT``
    or ``GCP_PROJECT`` for the project id.  Latest version is appended.
    """
    if source.startswith("projects/"):
        if "/versions/" not in source:
            return f"{source}/versions/latest"
        return source

    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
    if not project:
        raise SecretMissingError(
            source,
            "gcp-secret-manager",
            detail="short name supplied but GOOGLE_CLOUD_PROJECT is not set",
        )
    return f"projects/{project}/secrets/{source}/versions/latest"


class GcpSecretManagerResolver:
    provider: SecretProvider = "gcp-secret-manager"

    def __init__(self) -> None:
        self._client: object | None = None

    @require_extra(
        "secrets-gcp",
        ["google.cloud.secretmanager"],
        feature="GCP Secret Manager",
    )
    async def resolve(self, spec: SecretSpec) -> ResolvedSecret:
        try:
            path = _normalise_path(spec.source)
        except SecretMissingError:
            if spec.required:
                raise
            return ResolvedSecret(name=spec.name, value=None, provider=self.provider)

        from google.cloud import secretmanager  # type: ignore[import-not-found]

        if self._client is None:
            self._client = secretmanager.SecretManagerServiceAsyncClient()

        try:
            response = await self._client.access_secret_version(name=path)  # type: ignore[attr-defined]
            value = response.payload.data.decode("utf-8")
        except Exception as exc:  # noqa: BLE001 — wrap with Skaal context
            _LOG.warning("GCP Secret Manager fetch failed for %s: %s", spec.name, exc)
            if spec.required:
                raise SecretMissingError(
                    spec.name,
                    self.provider,
                    detail=f"AccessSecretVersion failed: {exc}",
                ) from exc
            return ResolvedSecret(name=spec.name, value=None, provider=self.provider)

        return ResolvedSecret(name=spec.name, value=value, provider=self.provider)

    async def close(self) -> None:
        if self._client is None:
            return
        close = getattr(self._client, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result
        self._client = None


__all__ = ["GcpSecretManagerResolver"]
