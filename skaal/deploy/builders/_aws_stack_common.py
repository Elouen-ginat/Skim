from __future__ import annotations

import re

# IAM managed policy ARN — stable AWS value, never changes.
LAMBDA_BASIC_EXEC_POLICY = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
LAMBDA_VPC_EXEC_POLICY = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"

# DynamoDB actions granted to the Lambda execution role.
DYNAMODB_ACTIONS = [
    "dynamodb:GetItem",
    "dynamodb:PutItem",
    "dynamodb:DeleteItem",
    "dynamodb:Scan",
    "dynamodb:Query",
]


def _resource_slug(name: str, *, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        slug = "skaal"
    if not slug[0].isalpha():
        slug = f"skaal-{slug}"
    return slug[:max_len].rstrip("-") or "skaal"


def _database_name(app_name: str) -> str:
    name = re.sub(r"[^a-z0-9]+", "_", app_name.lower()).strip("_")
    if not name:
        name = "skaal"
    if not name[0].isalpha():
        name = f"skaal_{name}"
    return name[:63]


def _apigw_path(path: str) -> str:
    """Convert a Skaal wildcard path to API Gateway v2 format."""
    if path in ("/*", "*"):
        return "/{proxy+}"
    if path.endswith("/*"):
        return path[:-2] + "/{proxy+}"
    if path.endswith("*"):
        return path[:-1] + "{proxy+}"
    return path


def _safe_key(route_key: str) -> str:
    """Stable Pulumi resource key derived from an API Gateway route key."""
    return re.sub(r"[^a-zA-Z0-9-]", "-", route_key).strip("-")


__all__ = [
    "DYNAMODB_ACTIONS",
    "LAMBDA_BASIC_EXEC_POLICY",
    "LAMBDA_VPC_EXEC_POLICY",
    "_apigw_path",
    "_database_name",
    "_resource_slug",
    "_safe_key",
]
