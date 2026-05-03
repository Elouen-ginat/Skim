"""AWS Secrets Manager runtime resolver.

Lambda env carries the secret's ARN (deploy-time wiring); this resolver
calls ``GetSecretValue`` on first access and caches in the registry.
"""

from __future__ import annotations

import logging
import os

from skaal.errors import SecretMissingError, require_extra
from skaal.types.secret import ResolvedSecret, SecretProvider, SecretSpec

_LOG = logging.getLogger("skaal.secrets.aws")


class AwsSecretsManagerResolver:
    """Async resolver backed by ``aioboto3``."""

    provider: SecretProvider = "aws-secrets-manager"

    def __init__(self, *, region: str | None = None) -> None:
        self._region = (
            region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        )
        self._session: object | None = None

    @require_extra("secrets-aws", ["aioboto3"], feature="AWS Secrets Manager")
    async def resolve(self, spec: SecretSpec) -> ResolvedSecret:
        secret_id = os.environ.get(spec.env) or spec.source
        if not secret_id:
            return ResolvedSecret(name=spec.name, value=None, provider=self.provider)

        import aioboto3  # type: ignore[import-not-found]

        if self._session is None:
            self._session = aioboto3.Session()

        async with self._session.client(  # type: ignore[attr-defined]
            "secretsmanager", region_name=self._region
        ) as client:
            try:
                response = await client.get_secret_value(SecretId=secret_id)
            except Exception as exc:  # noqa: BLE001 — wrap with Skaal context
                _LOG.warning("AWS Secrets Manager fetch failed for %s: %s", spec.name, exc)
                if spec.required:
                    raise SecretMissingError(
                        spec.name,
                        self.provider,
                        detail=f"GetSecretValue failed: {exc}",
                    ) from exc
                return ResolvedSecret(name=spec.name, value=None, provider=self.provider)

        value = response.get("SecretString")
        if value is None and "SecretBinary" in response:
            value = response["SecretBinary"].decode("utf-8")
        return ResolvedSecret(name=spec.name, value=value, provider=self.provider)

    async def close(self) -> None:
        # aioboto3 sessions are lightweight; the per-call client context handles cleanup.
        self._session = None


__all__ = ["AwsSecretsManagerResolver"]
