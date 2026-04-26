"""Vector-specific deploy wiring and Pulumi stack tests."""

from __future__ import annotations

from skaal.app import App
from skaal.deploy.builders.aws_stack import _build_pulumi_stack as build_aws_stack
from skaal.deploy.builders.gcp_stack import _build_pulumi_stack as build_gcp_stack
from skaal.deploy.wiring import build_runtime_wiring
from skaal.plan import PlanFile, StorageSpec


def _pgvector_wire() -> dict[str, object]:
    return {
        "class_name": "PgVectorBackend",
        "module": "skaal.backends.vector.pgvector",
        "env_prefix": "SKAAL_DB_DSN",
        "uses_namespace": True,
        "requires_vpc": True,
    }


def _cloud_sql_deploy() -> dict[str, object]:
    return {
        "database_version": "POSTGRES_16",
        "tier": "db-f1-micro",
        "backup_enabled": True,
        "deletion_protection": False,
    }


def _rds_deploy() -> dict[str, object]:
    return {
        "engine_version": "16.3",
        "instance_class": "db.t4g.micro",
        "allocated_storage_gb": 20,
        "max_allocated_storage_gb": 100,
        "storage_type": "gp3",
        "username": "skaal",
        "port": 5432,
        "backup_retention_days": 7,
        "deletion_protection": False,
    }


def test_local_build_falls_back_to_chroma_for_cloud_vector_backend() -> None:
    plan = PlanFile(
        app_name="demo",
        storage={
            "demo.Knowledge": StorageSpec(
                variable_name="demo.Knowledge",
                backend="cloud-sql-pgvector",
                kind="vector",
                wire_params=_pgvector_wire(),
            )
        },
    )

    imports, overrides = build_runtime_wiring(plan, target="local")

    assert "from skaal.backends.vector.chroma import ChromaVectorBackend" in imports
    assert (
        '"Knowledge": ChromaVectorBackend("/app/data/chroma", namespace="Knowledge"),' in overrides
    )


def test_build_runtime_wiring_aws_uses_pgvector_backend() -> None:
    plan = PlanFile(
        app_name="demo",
        storage={
            "demo.Knowledge": StorageSpec(
                variable_name="demo.Knowledge",
                backend="rds-pgvector",
                kind="vector",
                wire_params=_pgvector_wire(),
            )
        },
    )

    imports, overrides = build_runtime_wiring(plan, target="aws")

    assert "from skaal.backends.vector.pgvector import PgVectorBackend" in imports
    assert (
        '"Knowledge": PgVectorBackend(os.environ["SKAAL_DB_DSN_KNOWLEDGE"], namespace="Knowledge"),'
        in overrides
    )


def test_gcp_pulumi_stack_provisions_cloud_sql_pgvector() -> None:
    app = App(name="demo")
    plan = PlanFile(
        app_name="demo",
        deploy_target="gcp",
        storage={
            "demo.Knowledge": StorageSpec(
                variable_name="demo.Knowledge",
                backend="cloud-sql-pgvector",
                kind="vector",
                deploy_params=_cloud_sql_deploy(),
                wire_params=_pgvector_wire(),
            )
        },
    )

    stack = build_gcp_stack(app, plan, region="us-central1")
    resources = stack["resources"]

    assert resources["knowledge-sql"]["type"] == "gcp:sql:DatabaseInstance"
    assert resources["knowledge-db"]["type"] == "gcp:sql:Database"
    assert resources["vpc-connector"]["type"] == "gcp:vpcaccess:Connector"

    envs = resources["cloud-run-service"]["properties"]["template"]["spec"]["containers"][0]["envs"]
    vector_env = next(entry for entry in envs if entry["name"] == "SKAAL_DB_DSN_KNOWLEDGE")
    assert vector_env["value"] == (
        "postgresql://skaal@localhost/demo?host=/cloudsql/${knowledge-sql.connectionName}"
    )


def test_aws_pulumi_stack_provisions_rds_pgvector() -> None:
    app = App(name="demo")
    plan = PlanFile(
        app_name="demo",
        deploy_target="aws",
        storage={
            "demo.Knowledge": StorageSpec(
                variable_name="demo.Knowledge",
                backend="rds-pgvector",
                kind="vector",
                deploy_params=_rds_deploy(),
                wire_params=_pgvector_wire(),
            )
        },
    )

    stack = build_aws_stack(app, plan, region="us-east-1")
    resources = stack["resources"]

    assert resources["knowledge-db"]["type"] == "aws:rds:Instance"
    lambda_env = resources["lambda-fn"]["properties"]["environment"]["variables"]
    assert lambda_env["SKAAL_DB_DSN_KNOWLEDGE"].startswith(
        "postgresql://skaal:${knowledge-db-password.result}@"
    )
