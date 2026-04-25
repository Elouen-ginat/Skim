from skaal.deploy.targets.aws_lambda import target as aws_lambda
from skaal.deploy.targets.gcp_cloud_run import target as gcp_cloud_run
from skaal.deploy.targets.local_compose import target as local_compose

BUILTIN_TARGETS = (aws_lambda, gcp_cloud_run, local_compose)

__all__ = ["BUILTIN_TARGETS"]
