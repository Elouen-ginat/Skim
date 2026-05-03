"""Tests for the deploy-time SecretInjector implementations."""

from __future__ import annotations

from skaal.deploy.secrets import (
    AwsSecretInjector,
    GcpSecretInjector,
    LocalSecretInjector,
)
from skaal.plan import PlanFile
from skaal.types.secret import SecretSpec


def _plan_with(*specs: SecretSpec) -> PlanFile:
    return PlanFile(app_name="demo", secrets={s.name: s for s in specs})


def test_local_injector_emits_env_passthrough():
    plan = _plan_with(
        SecretSpec(name="DB", provider="env", source="DB", env="DB", required=True),
    )
    envs = LocalSecretInjector().env_vars(plan)
    assert envs == {"DB": "${env:DB}"}
    assert LocalSecretInjector().iam_statements(plan) == []


def test_aws_injector_env_carries_arn_for_secrets_manager():
    plan = _plan_with(
        SecretSpec(
            name="DB",
            provider="aws-secrets-manager",
            source="arn:aws:secretsmanager:us-east-1:123:secret:db-AbCdEf",
            env="DB",
            required=True,
        ),
        SecretSpec(name="API_KEY", provider="env", source="API_KEY", env="API_KEY", required=True),
    )
    inj = AwsSecretInjector()
    envs = inj.env_vars(plan)
    assert envs["DB"] == "arn:aws:secretsmanager:us-east-1:123:secret:db-AbCdEf"
    assert envs["API_KEY"] == "${env:API_KEY}"


def test_aws_injector_iam_statements_scope_to_arns_only():
    arn = "arn:aws:secretsmanager:us-east-1:123:secret:db-AbCdEf"
    plan = _plan_with(
        SecretSpec(name="DB", provider="aws-secrets-manager", source=arn, env="DB", required=True),
        SecretSpec(name="K", provider="env", source="K", env="K", required=True),
    )
    statements = AwsSecretInjector().iam_statements(plan)
    assert len(statements) == 1
    statement = statements[0]
    assert statement["Effect"] == "Allow"
    assert "secretsmanager:GetSecretValue" in statement["Action"]
    assert statement["Resource"] == [arn]


def test_aws_injector_no_iam_when_no_secrets_manager():
    plan = _plan_with(
        SecretSpec(name="K", provider="env", source="K", env="K", required=True),
    )
    assert AwsSecretInjector().iam_statements(plan) == []


def test_gcp_injector_emits_secret_sentinel_for_managed_secrets():
    plan = _plan_with(
        SecretSpec(
            name="DB",
            provider="gcp-secret-manager",
            source="projects/my-proj/secrets/db",
            env="DB",
            required=True,
        ),
    )
    envs = GcpSecretInjector().env_vars(plan)
    assert envs["DB"].startswith(GcpSecretInjector.SECRET_SENTINEL)
    assert envs["DB"].endswith("projects/my-proj/secrets/db")


def test_gcp_injector_emits_iam_member_request():
    plan = _plan_with(
        SecretSpec(
            name="DB",
            provider="gcp-secret-manager",
            source="projects/p/secrets/db",
            env="DB",
            required=True,
        ),
    )
    statements = GcpSecretInjector().iam_statements(plan)
    assert statements[0]["kind"] == "gcp-secret-iam-member"
    assert statements[0]["secret"] == "projects/p/secrets/db"
    assert statements[0]["role"] == "roles/secretmanager.secretAccessor"


def test_aws_builder_attaches_secrets_policy_when_secrets_manager_used():
    """Smoke test that the AWS builder wires the secrets IAM policy + attachment."""
    from skaal import App
    from skaal.deploy.builders.aws import build_pulumi_stack

    app = App("demo")
    arn = "arn:aws:secretsmanager:us-east-1:123:secret:db-Xy"
    plan = PlanFile(
        app_name="demo",
        deploy_target="aws-lambda",
        secrets={
            "DB": SecretSpec(
                name="DB",
                provider="aws-secrets-manager",
                source=arn,
                env="DB",
                required=True,
            )
        },
    )
    stack = build_pulumi_stack(app, plan, region="us-east-1")
    resources = stack["resources"]
    assert "secrets-policy" in resources
    assert "lambda-secrets-attach" in resources
    statement = resources["secrets-policy"]["properties"]["policy"]["fn::toJSON"]["Statement"][0]
    assert statement["Resource"] == [arn]


def test_gcp_builder_attaches_secret_iam_member():
    """Smoke test that the GCP builder wires SecretIamMember bindings."""
    from skaal import App
    from skaal.deploy.builders.gcp import build_pulumi_stack

    app = App("demo")
    plan = PlanFile(
        app_name="demo",
        deploy_target="gcp-cloudrun",
        secrets={
            "DB": SecretSpec(
                name="DB",
                provider="gcp-secret-manager",
                source="projects/p/secrets/db",
                env="DB",
                required=True,
            )
        },
    )
    stack = build_pulumi_stack(app, plan, region="us-central1")
    iam_resources = [key for key in stack["resources"] if "secret-iam-" in key]
    assert iam_resources, "expected at least one SecretIamMember resource"
    envs = stack["resources"]["cloud-run-service"]["properties"]["template"]["spec"]["containers"][
        0
    ]["envs"]
    db_env = next(entry for entry in envs if entry["name"] == "DB")
    assert "valueFrom" in db_env
    assert db_env["valueFrom"]["secretKeyRef"]["name"] == "projects/p/secrets/db"
