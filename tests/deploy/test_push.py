"""Tests for deploy helper integration points."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from skaal.deploy.packaging.docker_builder import build_image
from skaal.deploy.packaging.gcp_push import build_and_push_image
from skaal.deploy.packaging.local import build_local_image
from skaal.deploy.pulumi import DeploymentContext, RunnerPlan
from skaal.deploy.pulumi.automation import read_stack_spec, workspace_options, write_stack_spec
from skaal.deploy.pulumi.env import pulumi_env
from skaal.deploy.pulumi.runner import AutomationRunner


def test_pulumi_env_injects_blank_pulumi_passphrase(monkeypatch):
    monkeypatch.delenv("PULUMI_CONFIG_PASSPHRASE", raising=False)
    env = pulumi_env()
    assert env["PULUMI_CONFIG_PASSPHRASE"] == ""


def test_pulumi_env_preserves_existing_pulumi_passphrase(monkeypatch):
    monkeypatch.setenv("PULUMI_CONFIG_PASSPHRASE", "secret")
    env = pulumi_env()
    assert env["PULUMI_CONFIG_PASSPHRASE"] == "secret"


def test_write_stack_spec_round_trips(tmp_path: Path):
    spec = {
        "name": "skaal-demo",
        "runtime": "yaml",
        "config": {"localImageRef": {"type": "string", "default": "skaal-demo:local"}},
        "resources": {},
        "outputs": {"appUrl": "http://localhost:8000"},
    }

    path = write_stack_spec(tmp_path, spec)

    assert path.name == "skaal-stack.json"
    assert read_stack_spec(tmp_path) == spec


def test_workspace_options_creates_state_dir(tmp_path: Path):
    spec = {
        "name": "skaal-demo",
        "runtime": "yaml",
        "config": {},
        "resources": {},
        "outputs": {},
    }

    options = workspace_options(tmp_path, spec)

    assert (tmp_path / ".pulumi-state").is_dir()
    assert options.work_dir == str(tmp_path)


def test_build_local_image_uses_docker_builder(monkeypatch, tmp_path: Path):
    calls: list[tuple[Path, str]] = []

    def fake_build_image(*, context_dir: Path, tag: str, progress=None):
        del progress
        calls.append((context_dir, tag))
        return "sha256:test-image"

    monkeypatch.setattr("skaal.deploy.packaging.local.build_image", fake_build_image)

    image_ref = build_local_image(tmp_path, "skaal-test:local")

    assert image_ref == "sha256:test-image"
    assert calls == [(tmp_path.resolve(), "skaal-test:local")]


def test_build_image_uses_high_level_decoded_log_stream(monkeypatch, tmp_path: Path):
    chunks: list[dict[str, object]] = []
    captured_kwargs: dict[str, object] = {}

    class _FakeImages:
        def build(self, **kwargs):
            captured_kwargs.update(kwargs)
            return MagicMock(id="sha256:test-image"), iter(
                [
                    {"stream": "Step 1/2"},
                    {"stream": "Step 2/2"},
                ]
            )

    class _FakeClient:
        def __init__(self) -> None:
            self.images = _FakeImages()

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "skaal.deploy.packaging.docker_builder.docker.from_env", lambda: _FakeClient()
    )

    image_ref = build_image(
        context_dir=tmp_path,
        tag="skaal-test:local",
        progress=chunks.append,
    )

    assert image_ref == "sha256:test-image"
    assert captured_kwargs == {
        "path": str(tmp_path),
        "tag": "skaal-test:local",
        "rm": True,
        "forcerm": True,
    }
    assert chunks == [{"stream": "Step 1/2"}, {"stream": "Step 2/2"}]


def test_gcp_push_uses_google_auth_and_docker_builder(monkeypatch, tmp_path: Path):
    calls: list[tuple[str, object]] = []

    class _FakeCredentials:
        token: str | None = None

        def refresh(self, request) -> None:
            calls.append(("refresh", request))
            self.token = "test-token"

    monkeypatch.setattr(
        "skaal.deploy.packaging.gcp_push.google.auth.default",
        lambda *, scopes: (_FakeCredentials(), "demo-project"),
    )
    monkeypatch.setattr("skaal.deploy.packaging.gcp_push.Request", lambda: "request")
    monkeypatch.setattr(
        "skaal.deploy.packaging.gcp_push.login_registry",
        lambda **kwargs: calls.append(("login", kwargs)),
    )
    monkeypatch.setattr(
        "skaal.deploy.packaging.gcp_push.build_image",
        lambda **kwargs: calls.append(("build", kwargs)) or "sha256:test-image",
    )
    monkeypatch.setattr(
        "skaal.deploy.packaging.gcp_push.push_image",
        lambda **kwargs: calls.append(("push", kwargs)),
    )

    build_and_push_image(tmp_path, "demo-project", "us-central1", "repo-name", "demo-app")

    assert calls == [
        ("refresh", "request"),
        (
            "login",
            {
                "registry": "us-central1-docker.pkg.dev",
                "username": "oauth2accesstoken",
                "password": "test-token",
            },
        ),
        (
            "build",
            {
                "context_dir": tmp_path.resolve(),
                "tag": "us-central1-docker.pkg.dev/demo-project/repo-name/demo-app:latest",
                "progress": None,
            },
        ),
        (
            "push",
            {
                "repository": "us-central1-docker.pkg.dev/demo-project/repo-name/demo-app",
                "tag": "latest",
                "progress": None,
            },
        ),
    ]


def test_automation_runner_applies_package_config(monkeypatch):
    stack_ref = MagicMock()
    stack_ref.outputs.return_value = {"appUrl": MagicMock(value="http://localhost:8000")}

    monkeypatch.setattr(
        "skaal.deploy.pulumi.runner.read_stack_spec",
        lambda artifacts_dir: {
            "name": "skaal-test",
            "runtime": "yaml",
            "config": {},
            "resources": {},
            "outputs": {"appUrl": "http://localhost:8000"},
        },
    )
    monkeypatch.setattr(
        "skaal.deploy.pulumi.runner.workspace_options",
        lambda artifacts_dir, spec: MagicMock(),
    )
    monkeypatch.setattr(
        "skaal.deploy.pulumi.runner.auto.create_or_select_stack",
        lambda **kwargs: stack_ref,
    )

    runner = AutomationRunner()
    outputs = runner.deploy(
        RunnerPlan(
            context=DeploymentContext(
                target="local",
                artifacts_dir=Path("artifacts"),
                stack="local",
                region=None,
                gcp_project=None,
                yes=True,
                project_root=Path("."),
                source_module="examples.counter",
                app_name="Test App",
            ),
            config={"customConfig": "enabled"},
            package=lambda context: {"localImageRef": "sha256:local-image"},
            output_keys=("appUrl",),
        )
    )

    assert outputs == {"appUrl": "http://localhost:8000"}
    applied = {(call.args[0], call.args[1].value) for call in stack_ref.set_config.call_args_list}
    assert applied == {
        ("customConfig", "enabled"),
        ("localImageRef", "sha256:local-image"),
    }
    stack_ref.up.assert_called_once()
