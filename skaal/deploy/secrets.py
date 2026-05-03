"""Deploy-time secret injection.

Each cloud target gets a :class:`SecretInjector` that returns:

- ``env_vars(plan)``    — env-var → value mapping for the function spec.
- ``iam_statements(plan)`` — Pulumi statements that grant the function role
                             read access to the secret resources.

Both shapes are flat dicts / lists so builders can splice them into existing
Pulumi resource bodies without restructuring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from skaal.types.secret import SecretGrant, SecretSpec

if TYPE_CHECKING:
    from skaal.plan import PlanFile


# ── Protocol ──────────────────────────────────────────────────────────────────


class SecretInjector(Protocol):
    """Strategy that wires :class:`SecretSpec` declarations into a deploy artifact."""

    def env_vars(self, plan: "PlanFile") -> dict[str, str]: ...

    def iam_statements(self, plan: "PlanFile") -> list[dict[str, Any]]: ...

    def grants(self, plan: "PlanFile") -> list[SecretGrant]: ...


def _iter_secrets(plan: "PlanFile") -> list[SecretSpec]:
    return list(plan.secrets.values())


# ── Local ────────────────────────────────────────────────────────────────────


class LocalSecretInjector:
    """Local / docker-compose injector — every secret reads from the host env.

    The runtime resolver is :class:`~skaal.secrets.EnvResolver`; here we only
    need to make sure the variable name appears in the rendered env block so
    docker-compose forwards it from the host shell.
    """

    def env_vars(self, plan: "PlanFile") -> dict[str, str]:
        return {spec.env: f"${{env:{spec.env}}}" for spec in _iter_secrets(plan)}

    def iam_statements(self, plan: "PlanFile") -> list[dict[str, Any]]:
        return []

    def grants(self, plan: "PlanFile") -> list[SecretGrant]:
        return [
            SecretGrant(provider=spec.provider, resource_id=spec.env)
            for spec in _iter_secrets(plan)
        ]


# ── AWS ──────────────────────────────────────────────────────────────────────


_AWS_SECRET_ACTIONS = (
    "secretsmanager:GetSecretValue",
    "secretsmanager:DescribeSecret",
)


class AwsSecretInjector:
    """AWS Lambda injector — Secrets Manager + env passthrough.

    - ``provider == "aws-secrets-manager"`` → env carries the ARN; IAM grant
      scoped to that ARN.
    - ``provider == "env"`` / ``"pulumi-config"`` → env carries the value
      directly via ``${env:NAME}`` or Pulumi config substitution; no IAM.
    """

    def env_vars(self, plan: "PlanFile") -> dict[str, str]:
        out: dict[str, str] = {}
        for spec in _iter_secrets(plan):
            if spec.provider == "aws-secrets-manager":
                out[spec.env] = spec.source  # ARN; runtime fetches via SDK
            else:
                out[spec.env] = f"${{env:{spec.env}}}"
        return out

    def iam_statements(self, plan: "PlanFile") -> list[dict[str, Any]]:
        arns = [
            spec.source for spec in _iter_secrets(plan) if spec.provider == "aws-secrets-manager"
        ]
        if not arns:
            return []
        return [
            {
                "Effect": "Allow",
                "Action": list(_AWS_SECRET_ACTIONS),
                "Resource": arns,
            }
        ]

    def grants(self, plan: "PlanFile") -> list[SecretGrant]:
        return [
            SecretGrant(
                provider=spec.provider,
                resource_id=spec.source,
                actions=_AWS_SECRET_ACTIONS,
            )
            for spec in _iter_secrets(plan)
            if spec.provider == "aws-secrets-manager"
        ]


# ── GCP ──────────────────────────────────────────────────────────────────────


class GcpSecretInjector:
    """GCP Cloud Run injector — Secret Manager binding + env_from.

    - ``provider == "gcp-secret-manager"`` → emits a marker dict that the
      Cloud Run builder converts into ``env_from.secret_key_ref`` and a
      :class:`SecretIamMember` granting ``roles/secretmanager.secretAccessor``.
    - other providers → env passthrough.

    The Cloud Run builder consumes ``grants(plan)`` to render the IAM
    bindings; ``env_vars(plan)`` returns the literal env mapping for the
    non-managed providers and a sentinel ``SECRET:<resource>`` for the
    managed one (consumed by the builder, never passed through verbatim).
    """

    SECRET_SENTINEL = "SECRET:"

    def env_vars(self, plan: "PlanFile") -> dict[str, str]:
        out: dict[str, str] = {}
        for spec in _iter_secrets(plan):
            if spec.provider == "gcp-secret-manager":
                out[spec.env] = f"{self.SECRET_SENTINEL}{spec.source}"
            else:
                out[spec.env] = f"${{env:{spec.env}}}"
        return out

    def iam_statements(self, plan: "PlanFile") -> list[dict[str, Any]]:
        return [
            {
                "kind": "gcp-secret-iam-member",
                "secret": spec.source,
                "role": "roles/secretmanager.secretAccessor",
            }
            for spec in _iter_secrets(plan)
            if spec.provider == "gcp-secret-manager"
        ]

    def grants(self, plan: "PlanFile") -> list[SecretGrant]:
        return [
            SecretGrant(
                provider=spec.provider,
                resource_id=spec.source,
                actions=("read",),
            )
            for spec in _iter_secrets(plan)
            if spec.provider == "gcp-secret-manager"
        ]


__all__ = [
    "AwsSecretInjector",
    "GcpSecretInjector",
    "LocalSecretInjector",
    "SecretInjector",
]
