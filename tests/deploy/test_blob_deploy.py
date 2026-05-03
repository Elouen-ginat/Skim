"""Blob-specific deploy wiring and Pulumi stack tests."""

from __future__ import annotations

from skaal.app import App
from skaal.deploy.backends.wiring import build_wiring, build_wiring_aws
from skaal.deploy.builders.aws import build_pulumi_stack as build_aws_stack
from skaal.deploy.builders.gcp import build_pulumi_stack as build_gcp_stack
from skaal.plan import PlanFile, StorageSpec


def _s3_wire() -> dict[str, object]:
    return {
        "class_name": "S3BlobBackend",
        "module": "s3_blob_backend",
        "env_prefix": "SKAAL_S3_BUCKET",
        "uses_namespace": True,
        "extra_deps": ["s3fs>=2025.3.0"],
    }


def _gcs_wire() -> dict[str, object]:
    return {
        "class_name": "GCSBlobBackend",
        "module": "gcs_blob_backend",
        "env_prefix": "SKAAL_GCS_BUCKET",
        "uses_namespace": True,
        "extra_deps": ["gcsfs>=2025.3.0"],
    }


def test_local_build_falls_back_to_file_blob_backend_for_s3() -> None:
    plan = PlanFile(
        app_name="demo",
        storage={
            "demo.Uploads": StorageSpec(
                variable_name="demo.Uploads",
                backend="s3",
                kind="blob",
                wire_params=_s3_wire(),
            )
        },
    )

    imports, overrides = build_wiring(plan, local=True)

    assert "from skaal.backends.file_blob_backend import FileBlobBackend" in imports
    assert '"Uploads": FileBlobBackend("/app/data/blobs", namespace="Uploads"),' in overrides


def test_build_wiring_aws_uses_s3_blob_backend() -> None:
    plan = PlanFile(
        app_name="demo",
        storage={
            "demo.Uploads": StorageSpec(
                variable_name="demo.Uploads",
                backend="s3",
                kind="blob",
                wire_params=_s3_wire(),
            )
        },
    )

    imports, overrides = build_wiring_aws(plan)

    assert "from skaal.backends.s3_blob_backend import S3BlobBackend" in imports
    assert (
        '"Uploads": S3BlobBackend(os.environ["SKAAL_S3_BUCKET_UPLOADS"], namespace="Uploads"),'
        in overrides
    )


def test_gcp_pulumi_stack_provisions_gcs_bucket() -> None:
    app = App(name="demo")
    plan = PlanFile(
        app_name="demo",
        deploy_target="gcp",
        storage={
            "demo.Uploads": StorageSpec(
                variable_name="demo.Uploads",
                backend="gcs",
                kind="blob",
                wire_params=_gcs_wire(),
            )
        },
    )

    stack = build_gcp_stack(app, plan, region="us-central1")
    resources = stack["resources"]

    assert resources["uploads-bucket"]["type"] == "gcp:storage:Bucket"
    envs = resources["cloud-run-service"]["properties"]["template"]["spec"]["containers"][0]["envs"]
    blob_env = next(entry for entry in envs if entry["name"] == "SKAAL_GCS_BUCKET_UPLOADS")
    assert blob_env["value"] == "${pulumi.stack}-demo-uploads"


def test_aws_pulumi_stack_provisions_s3_bucket() -> None:
    app = App(name="demo")
    plan = PlanFile(
        app_name="demo",
        deploy_target="aws",
        storage={
            "demo.Uploads": StorageSpec(
                variable_name="demo.Uploads",
                backend="s3",
                kind="blob",
                wire_params=_s3_wire(),
            )
        },
    )

    stack = build_aws_stack(app, plan, region="us-east-1")
    resources = stack["resources"]

    assert resources["uploads-bucket"]["type"] == "aws:s3:BucketV2"
    lambda_env = resources["lambda-fn"]["properties"]["environment"]["variables"]
    assert lambda_env["SKAAL_S3_BUCKET_UPLOADS"] == "${pulumi.stack}-uploads"
    assert resources["s3-policy"]["type"] == "aws:iam:Policy"
