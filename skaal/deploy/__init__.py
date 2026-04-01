"""Pulumi Automation API integration — translates a PlanFile to infrastructure."""

from skaal.deploy.pulumi_backend import deploy_from_plan

__all__ = ["deploy_from_plan"]
