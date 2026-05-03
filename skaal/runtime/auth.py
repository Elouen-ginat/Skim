from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any, cast

import httpx
import jwt
from jwt import InvalidTokenError
from jwt.algorithms import RSAAlgorithm

from skaal.types import AuthConfig

_JWKS_CACHE_TTL_SECONDS = 300.0


class RuntimeAuthConfigError(ValueError):
    """Raised when attached auth config is invalid or conflicts."""


class RuntimeAuthFailure(RuntimeError):
    """Raised when a request fails auth validation."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _normalize_auth_config(raw: Mapping[str, Any]) -> AuthConfig:
    provider = raw.get("provider")
    if provider != "jwt":
        raise RuntimeAuthConfigError(
            f"Unsupported APIGateway.auth provider {provider!r}; only 'jwt' is implemented."
        )

    issuer = str(raw.get("issuer") or "").strip()
    if not issuer:
        raise RuntimeAuthConfigError("APIGateway.auth issuer is required for provider='jwt'.")

    config: AuthConfig = {
        "provider": "jwt",
        "issuer": issuer,
        "header": str(raw.get("header") or "Authorization"),
        "required": bool(raw.get("required", True)),
    }
    audience = raw.get("audience")
    if audience:
        config["audience"] = str(audience)
    return config


def resolve_gateway_auth(app: Any) -> AuthConfig | None:
    configs: list[AuthConfig] = []
    for component in getattr(app, "_components", {}).values():
        if getattr(component, "_skaal_component_kind", None) != "api-gateway":
            continue
        raw_auth = getattr(component, "__skaal_component__", {}).get("auth")
        if not raw_auth:
            continue
        config = _normalize_auth_config(cast(Mapping[str, Any], raw_auth))
        if config not in configs:
            configs.append(config)

    if not configs:
        return None
    if len(configs) > 1:
        raise RuntimeAuthConfigError(
            "Conflicting APIGateway.auth configs are attached to this app. "
            "Use exactly one effective JWT auth policy."
        )
    return configs[0]


class JwtVerifier:
    def __init__(self, config: AuthConfig, *, http_client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._http_client = http_client
        self._jwks: list[dict[str, Any]] = []
        self._jwks_loaded_at: float = 0.0
        self.ready: bool = False
        self.last_error: str | None = None

    @property
    def header_name(self) -> str:
        return self.config.get("header", "Authorization")

    @property
    def required(self) -> bool:
        return bool(self.config.get("required", True))

    @property
    def issuer(self) -> str:
        return self.config["issuer"]

    @property
    def audience(self) -> str | None:
        return self.config.get("audience")

    @property
    def jwks_url(self) -> str:
        return self.issuer.rstrip("/") + "/.well-known/jwks.json"

    async def initialize(self) -> None:
        await self._refresh_jwks()

    async def verify_headers(self, headers: Mapping[str, str]) -> dict[str, Any] | None:
        raw_value = _lookup_header(headers, self.header_name)
        if raw_value is None:
            if self.required:
                raise RuntimeAuthFailure(401, f"Missing {self.header_name} header")
            return None

        token = _extract_token(self.header_name, raw_value)
        if not token:
            if self.required:
                raise RuntimeAuthFailure(401, f"Missing token in {self.header_name} header")
            return None

        if not self._jwks or self._jwks_is_stale:
            await self._refresh_jwks()

        try:
            unverified = cast(dict[str, Any], jwt.get_unverified_header(token))
            jwk = self._select_jwk(unverified.get("kid"))
            if jwk is None:
                await self._refresh_jwks()
                jwk = self._select_jwk(unverified.get("kid"))
            if jwk is None:
                raise RuntimeAuthFailure(403, "Unknown JWT signing key")

            algorithm = str(unverified.get("alg") or "RS256")
            key = RSAAlgorithm.from_jwk(json.dumps(jwk))
            claims = cast(
                dict[str, Any],
                jwt.decode(
                    token,
                    key=key,
                    algorithms=[algorithm],
                    issuer=self.issuer,
                    audience=self.audience,
                    options={"verify_aud": self.audience is not None},
                ),
            )
        except RuntimeAuthFailure:
            raise
        except InvalidTokenError as exc:
            raise RuntimeAuthFailure(403, f"Invalid token: {exc}") from exc

        self.last_error = None
        return claims

    @property
    def _jwks_is_stale(self) -> bool:
        return (time.time() - self._jwks_loaded_at) >= _JWKS_CACHE_TTL_SECONDS

    async def _refresh_jwks(self) -> None:
        try:
            payload = await self._fetch_jwks()
            keys = payload.get("keys")
            if not isinstance(keys, list) or not keys:
                raise RuntimeAuthConfigError(f"No JWKS keys found at {self.jwks_url!r}.")
            self._jwks = [cast(dict[str, Any], item) for item in keys if isinstance(item, dict)]
            if not self._jwks:
                raise RuntimeAuthConfigError(f"No usable JWKS keys found at {self.jwks_url!r}.")
            self._jwks_loaded_at = time.time()
            self.ready = True
            self.last_error = None
        except Exception as exc:
            self.ready = False
            self.last_error = str(exc)
            raise

    async def _fetch_jwks(self) -> dict[str, Any]:
        if self._http_client is not None:
            response = await self._http_client.get(self.jwks_url)
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            response = await client.get(self.jwks_url)
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

    def _select_jwk(self, kid: Any) -> dict[str, Any] | None:
        if kid is not None:
            for jwk in self._jwks:
                if jwk.get("kid") == kid:
                    return jwk
            return None
        return self._jwks[0] if self._jwks else None


def _lookup_header(headers: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    for header_name, header_value in headers.items():
        if header_name.lower() == target:
            return header_value
    return None


def _extract_token(header_name: str, header_value: str) -> str | None:
    value = header_value.strip()
    if not value:
        return None
    if header_name.lower() != "authorization":
        return value
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None
