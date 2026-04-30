"""Tests for per-stack settings profiles."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from skaal.settings import SkaalSettings, StackProfile
from skaal.types import StackProfile as DeployStackProfile


def _write_pyproject(tmp_path: Path, body: str) -> None:
    (tmp_path / "pyproject.toml").write_text(dedent(body))


def test_no_profile_returns_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """for_stack() on an unknown name returns a copy with only `stack` updated."""
    _write_pyproject(
        tmp_path,
        """
        [tool.skaal]
        target = "gcp"
        region = "europe-west1"
        gcp_project = "base-proj"
        """,
    )
    monkeypatch.chdir(tmp_path)

    resolved = SkaalSettings().for_stack("nonexistent")
    assert resolved.stack == "nonexistent"
    assert resolved.region == "europe-west1"
    assert resolved.gcp_project == "base-proj"


def test_profile_overrides_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fields on the profile win; unset fields fall through to the base."""
    _write_pyproject(
        tmp_path,
        """
        [tool.skaal]
        target = "gcp"
        region = "europe-west1"

        [tool.skaal.stacks.p-prd]
        gcp_project = "my-prd-proj"
        region      = "europe-west4"

        [tool.skaal.stacks.p-dev]
        gcp_project = "my-dev-proj"
        """,
    )
    monkeypatch.chdir(tmp_path)

    base = SkaalSettings()
    assert base.gcp_project is None
    assert set(base.stacks) == {"p-prd", "p-dev"}

    prd = base.for_stack("p-prd")
    assert prd.stack == "p-prd"
    assert prd.gcp_project == "my-prd-proj"
    assert prd.region == "europe-west4"

    dev = base.for_stack("p-dev")
    assert dev.stack == "p-dev"
    assert dev.gcp_project == "my-dev-proj"
    assert dev.region == "europe-west1"  # fell through


def test_for_stack_none_uses_current(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """for_stack(None) resolves the currently-selected stack."""
    _write_pyproject(
        tmp_path,
        """
        [tool.skaal]
        stack  = "p-ppr"
        target = "gcp"

        [tool.skaal.stacks.p-ppr]
        gcp_project = "my-ppr-proj"
        """,
    )
    monkeypatch.chdir(tmp_path)

    resolved = SkaalSettings().for_stack(None)
    assert resolved.stack == "p-ppr"
    assert resolved.gcp_project == "my-ppr-proj"


def test_unknown_profile_field_rejected() -> None:
    """Typos in profile keys are caught, not silently dropped."""
    with pytest.raises(ValueError):
        StackProfile.model_validate({"regionn": "oops"})


def test_env_var_beats_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SKAAL_* env vars live on the base settings and survive for_stack()
    when the profile does not override that field."""
    _write_pyproject(
        tmp_path,
        """
        [tool.skaal]
        target = "gcp"

        [tool.skaal.stacks.p-dev]
        gcp_project = "from-profile"
        """,
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SKAAL_REGION", "from-env")

    resolved = SkaalSettings().for_stack("p-dev")
    assert resolved.region == "from-env"
    assert resolved.gcp_project == "from-profile"


# ── Phase 2 — overrides + deletion_protection ────────────────────────────────


def test_profile_overrides_are_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Raw Pulumi overrides declared under [tool.skaal.stacks.X.overrides]
    are loaded and surface on the resolved settings."""
    _write_pyproject(
        tmp_path,
        """
        [tool.skaal]
        target = "gcp"

        [tool.skaal.stacks.p-prd.overrides]
        cloudRunMemory       = "1Gi"
        cloudRunMinInstances = 2
        """,
    )
    monkeypatch.chdir(tmp_path)

    prd = SkaalSettings().for_stack("p-prd")
    assert prd.overrides == {"cloudRunMemory": "1Gi", "cloudRunMinInstances": 2}


def test_deletion_protection_shortcut_on_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """deletion_protection on the profile propagates as a bool setting."""
    _write_pyproject(
        tmp_path,
        """
        [tool.skaal]
        target = "gcp"

        [tool.skaal.stacks.p-prd]
        deletion_protection = true
        """,
    )
    monkeypatch.chdir(tmp_path)

    prd = SkaalSettings().for_stack("p-prd")
    dev = SkaalSettings().for_stack("does-not-exist")
    assert prd.deletion_protection is True
    assert dev.deletion_protection is None


def test_build_config_overrides_expands_deletion_protection(tmp_path: Path) -> None:
    """_build_config_overrides() expands deletion_protection into one
    sqlDeletionProtection<Class> key per cloud-sql-postgres storage and
    leaves raw override values stringified."""
    from skaal.api import _build_config_overrides
    from skaal.plan import PlanFile, StorageSpec
    from skaal.settings import SkaalSettings

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    plan = PlanFile(
        app_name="demo",
        deploy_target="gcp",
        storage={
            "demo.Items": StorageSpec(
                variable_name="Items",
                backend="cloud-sql-postgres",
                deploy_params={},
            ),
            "demo.Cache": StorageSpec(
                variable_name="Cache",
                backend="firestore",
                deploy_params={},
            ),
        },
    )
    plan.write(tmp_path / "plan.skaal.lock")

    cfg = SkaalSettings(
        overrides={"cloudRunMemory": "1Gi", "cloudRunMinInstances": 2},
        deletion_protection=True,
    )

    overrides = _build_config_overrides(cfg, artifacts_dir)
    assert overrides == {
        "cloudRunMemory": "1Gi",
        "cloudRunMinInstances": "2",
        "sqlDeletionProtectionItems": "true",
    }


def test_enable_mesh_resolves_from_stack_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_pyproject(
        tmp_path,
        """
        [tool.skaal]
        enable_mesh = false

        [tool.skaal.stacks.mesh]
        enable_mesh = true
        """,
    )
    monkeypatch.chdir(tmp_path)

    base = SkaalSettings()
    assert base.enable_mesh is False
    assert base.for_stack("mesh").enable_mesh is True


def test_build_config_overrides_explicit_wins_over_shortcut(tmp_path: Path) -> None:
    """An explicit sqlDeletionProtection<Class> in overrides beats the
    deletion_protection shortcut."""
    from skaal.api import _build_config_overrides
    from skaal.plan import PlanFile, StorageSpec
    from skaal.settings import SkaalSettings

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    plan = PlanFile(
        app_name="demo",
        deploy_target="gcp",
        storage={
            "demo.Items": StorageSpec(
                variable_name="Items",
                backend="cloud-sql-postgres",
                deploy_params={},
            ),
        },
    )
    plan.write(tmp_path / "plan.skaal.lock")

    cfg = SkaalSettings(
        overrides={"sqlDeletionProtectionItems": "false"},
        deletion_protection=True,
    )

    overrides = _build_config_overrides(cfg, artifacts_dir)
    assert overrides == {"sqlDeletionProtectionItems": "false"}


# ── Phase 3 — env / invokers / labels / hooks / stacks cmd ──────────────────


def test_build_stack_profile_only_includes_nonempty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_build_stack_profile omits keys whose profile value is empty so
    generators can truthiness-check before emitting stack-specific YAML."""
    from skaal.api import _build_stack_profile

    _write_pyproject(
        tmp_path,
        """
        [tool.skaal]
        target = "gcp"

        [tool.skaal.stacks.p-prd]
        invokers = ["serviceAccount:svc@example.com"]

        [tool.skaal.stacks.p-prd.env]
        FEATURE_X = "on"

        [tool.skaal.stacks.p-prd.labels]
        env = "prd"

        [tool.skaal.stacks.p-dev]
        """,
    )
    monkeypatch.chdir(tmp_path)

    prd = _build_stack_profile(SkaalSettings().for_stack("p-prd"))
    assert prd == {
        "env": {"FEATURE_X": "on"},
        "invokers": ["serviceAccount:svc@example.com"],
        "labels": {"env": "prd"},
    }

    dev = _build_stack_profile(SkaalSettings().for_stack("p-dev"))
    assert dev == {}


def test_gcp_pulumi_stack_applies_profile() -> None:
    """env / invokers / labels from the profile land in the right places
    inside the generated Pulumi stack dict."""
    from skaal.app import App
    from skaal.deploy.builders.gcp import build_pulumi_stack
    from skaal.plan import PlanFile

    app = App(name="demo")
    plan = PlanFile(app_name="demo", deploy_target="gcp")
    profile: DeployStackProfile = {
        "env": {"FEATURE_X": "on", "FEATURE_Y": "off"},
        "invokers": [
            "serviceAccount:alice@example.com",
            "serviceAccount:bob@example.com",
        ],
        "labels": {"env": "prd", "team": "infra"},
    }

    stack = build_pulumi_stack(app, plan, region="europe-west1", stack_profile=profile)
    resources = stack["resources"]

    envs = resources["cloud-run-service"]["properties"]["template"]["spec"]["containers"][0]["envs"]
    env_map = {e["name"]: e["value"] for e in envs}
    assert env_map == {"FEATURE_X": "on", "FEATURE_Y": "off"}

    labels = resources["cloud-run-service"]["properties"]["template"]["metadata"]["labels"]
    assert labels == {"env": "prd", "team": "infra"}

    # One IamMember per invoker; the first keeps the canonical key, subsequent
    # entries get a numeric suffix.
    assert resources["invoker"]["properties"]["member"] == ("serviceAccount:alice@example.com")
    assert resources["invoker-1"]["properties"]["member"] == ("serviceAccount:bob@example.com")


def test_gcp_pulumi_stack_defaults_to_public_invoker() -> None:
    """With no invokers profile key, the invoker resource stays public."""
    from skaal.app import App
    from skaal.deploy.builders.gcp import build_pulumi_stack
    from skaal.plan import PlanFile

    app = App(name="demo")
    plan = PlanFile(app_name="demo", deploy_target="gcp")

    stack = build_pulumi_stack(app, plan, region="us-central1")
    assert stack["resources"]["invoker"]["properties"]["member"] == "allUsers"
    assert "invoker-1" not in stack["resources"]


def test_run_hooks_sets_output_env(tmp_path: Path) -> None:
    """post_deploy hooks receive Pulumi outputs as SKAAL_OUTPUT_* env vars."""
    import sys

    from skaal.api import _run_hooks

    marker = tmp_path / "captured.txt"
    # Use the current Python interpreter to avoid Windows PATH issues.
    cmd = [
        sys.executable,
        "-c",
        "import os, pathlib; "
        f"pathlib.Path(r'{marker}').write_text(os.environ['SKAAL_OUTPUT_SERVICE_URL'])",
    ]

    _run_hooks([cmd], cwd=tmp_path, extra_env={"SKAAL_OUTPUT_SERVICE_URL": "https://x"})
    assert marker.read_text() == "https://x"


def test_run_hooks_propagates_failure(tmp_path: Path) -> None:
    """A non-zero exit aborts the hook sequence with CalledProcessError."""
    import subprocess
    import sys

    from skaal.api import _run_hooks

    bad = [sys.executable, "-c", "import sys; sys.exit(7)"]
    with pytest.raises(subprocess.CalledProcessError):
        _run_hooks([bad], cwd=tmp_path)


def test_stacks_cli_lists_profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`skaal stacks` prints one row per declared profile."""
    from typer.testing import CliRunner

    from skaal.cli.stacks_cmd import app as stacks_app

    _write_pyproject(
        tmp_path,
        """
        [tool.skaal]
        target = "gcp"
        region = "europe-west1"
        stack  = "p-dev"

        [tool.skaal.stacks.p-dev]
        gcp_project = "dev-proj"

        [tool.skaal.stacks.p-prd]
        gcp_project         = "prd-proj"
        region              = "europe-west4"
        deletion_protection = true
        pre_deploy          = [["echo", "hi"]]
        """,
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(stacks_app, [])
    assert result.exit_code == 0, result.stdout
    assert "p-dev" in result.stdout
    assert "p-prd" in result.stdout
    assert "dev-proj" in result.stdout
    assert "prd-proj" in result.stdout
    assert "europe-west4" in result.stdout
