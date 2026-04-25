"""Tests for the :mod:`skaal.api` Python API."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from skaal import App, api

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def simple_app() -> App:
    """Build a minimal Skaal App with one storage class and one function."""
    app = App(name="test-app")

    @app.storage
    class Counter:
        pass

    @app.function
    async def ping() -> dict[str, str]:
        return {"message": "pong"}

    return app


@pytest.fixture
def tmp_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """chdir into *tmp_path* with a ``catalogs/local.toml`` for solving."""
    catalog_dir = tmp_path / "catalogs"
    catalog_dir.mkdir()
    (catalog_dir / "local.toml").write_text(
        """
[storage.local-map]
display_name = "Local Memory"
read_latency = { min = 0.001, max = 0.1, unit = "ms" }
write_latency = { min = 0.001, max = 0.1, unit = "ms" }
durability = ["ephemeral", "persistent"]
access_patterns = ["random-read", "random-write", "sequential"]
cost_per_gb_month = 0.001
max_size_gb = 0
"""
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ── resolve_app / load_app ────────────────────────────────────────────────────


def test_resolve_app_accepts_instance(simple_app: App) -> None:
    """Passing a live App instance returns it unchanged."""
    assert api.resolve_app(simple_app) is simple_app


def test_resolve_app_rejects_wrong_type() -> None:
    """Non-App, non-string arguments raise TypeError."""
    with pytest.raises(TypeError, match="App reference"):
        api.resolve_app(42)  # type: ignore[arg-type]


def test_load_app_missing_colon() -> None:
    """A reference string without ':' raises ValueError."""
    with pytest.raises(ValueError, match="module:variable"):
        api.load_app("not_a_reference")


def test_load_app_missing_module() -> None:
    """Unimportable modules raise ModuleNotFoundError."""
    with pytest.raises(ModuleNotFoundError):
        api.load_app("definitely_not_a_real_module:app")


def test_load_app_missing_attribute(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reference to a missing attribute raises AttributeError."""
    (tmp_project / "dummy_mod.py").write_text("value = 1\n")
    monkeypatch.syspath_prepend(str(tmp_project))
    with pytest.raises(AttributeError):
        api.load_app("dummy_mod:nonexistent")


# ── plan ──────────────────────────────────────────────────────────────────────


def test_plan_returns_plan_file(simple_app: App, tmp_project: Path) -> None:
    """plan() returns a PlanFile and writes plan.skaal.lock by default."""
    plan_file = api.plan(simple_app, target="local")

    from skaal.plan import PLAN_FILE_NAME, PlanFile

    assert plan_file.app_name == "test-app"
    assert plan_file.deploy_target == "local"
    assert (tmp_project / PLAN_FILE_NAME).exists()

    # Round-trip through disk must yield the same app name.
    reloaded = PlanFile.read(tmp_project / PLAN_FILE_NAME)
    assert reloaded.app_name == plan_file.app_name


def test_plan_no_write(simple_app: App, tmp_project: Path) -> None:
    """plan(..., write=False) does not touch the filesystem."""
    api.plan(simple_app, target="local", write=False)
    assert not (tmp_project / "plan.skaal.lock").exists()


def test_plan_custom_output_path(simple_app: App, tmp_project: Path) -> None:
    """plan() honours an explicit output path."""
    out = tmp_project / "custom.lock"
    api.plan(simple_app, target="local", output_path=out)
    assert out.exists()


def test_plan_missing_catalog(
    simple_app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing catalog surfaces as FileNotFoundError."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        api.plan(simple_app, target="local", catalog=tmp_path / "nope.toml")


def test_plan_records_source_module_from_string_ref(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """String refs should propagate into plan.source_module / app_var."""
    # Write a tiny app module in the temp project.
    (tmp_project / "my_app.py").write_text(
        "from skaal import App\n"
        "app = App(name='hello')\n"
        "@app.storage\n"
        "class Thing:\n"
        "    pass\n"
    )
    monkeypatch.syspath_prepend(str(tmp_project))

    plan_file = api.plan("my_app:app", target="local")
    assert plan_file.source_module == "my_app"
    assert plan_file.app_var == "app"


# ── catalog ───────────────────────────────────────────────────────────────────


def test_catalog_returns_typed_object(tmp_project: Path) -> None:
    """catalog() returns a validated Catalog with expected sections."""
    cat = api.catalog(tmp_project / "catalogs" / "local.toml")
    assert "local-map" in cat.storage


def test_catalog_missing_file_raises(tmp_path: Path) -> None:
    """catalog() raises FileNotFoundError for unknown paths."""
    with pytest.raises(FileNotFoundError):
        api.catalog(tmp_path / "nope.toml")


# ── build ─────────────────────────────────────────────────────────────────────


def test_build_raises_without_plan(tmp_project: Path) -> None:
    """build() without a plan file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        api.build()


def test_build_delegates_to_target(
    simple_app: App, tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """build() delegates to the deploy build pipeline."""
    plan_file = api.plan(simple_app, target="local", write=True)

    expected_paths = [tmp_project / "artifacts" / "main.py"]

    def _fake_build_artifacts(**kwargs):
        assert kwargs["plan"].app_name == plan_file.app_name
        return expected_paths

    monkeypatch.setattr("skaal.deploy.build_artifacts", _fake_build_artifacts)

    generated = api.build(app=simple_app, output_dir=tmp_project / "artifacts")
    assert generated == expected_paths


# ── diff ──────────────────────────────────────────────────────────────────────


def test_diff_empty_when_no_new_plan(simple_app: App, tmp_project: Path) -> None:
    """Calling diff() with no new plan returns an empty diff."""
    api.plan(simple_app, target="local")
    result = api.diff()
    assert result.has_changes is False
    assert result.storage == []
    assert result.compute == []


def test_diff_detects_added_storage(simple_app: App, tmp_project: Path) -> None:
    """Re-solving against an app with extra storage yields an 'added' entry."""
    original = api.plan(simple_app, target="local", write=True)

    # Mutate a second app with an extra storage class.
    extended = App(name="test-app")

    @extended.storage
    class Counter:
        pass

    @extended.storage
    class Profiles:
        pass

    new_plan = api.plan(extended, target="local", write=False)

    result = api.diff(new_plan=new_plan, old_plan=original)
    added_names = {e.name for e in result.storage if e.change == "added"}
    assert any("Profiles" in name for name in added_names), added_names


# ── infra ─────────────────────────────────────────────────────────────────────


def test_infra_status_returns_snapshot(simple_app: App, tmp_project: Path) -> None:
    """infra_status() returns an InfraStatus wrapping the PlanFile."""
    api.plan(simple_app, target="local")
    snapshot = api.infra_status()

    assert snapshot.app_name == "test-app"
    assert snapshot.deploy_target == "local"
    assert isinstance(snapshot.storage, dict)
    # No migrations set up in this test.
    assert snapshot.migrations == {}


def test_infra_status_missing_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing plan file raises FileNotFoundError."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        api.infra_status()


# ── migrate ───────────────────────────────────────────────────────────────────


@pytest.fixture
def migration_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated cwd for migration tests (creates .skaal/migrations/<app>/)."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_migrate_start_and_status(migration_sandbox: Path) -> None:
    """migrate_start() persists state retrievable via migrate_status()."""
    state = api.migrate_start(
        "demo.Counts",
        from_backend="elasticache-redis",
        to_backend="dynamodb",
        app_name="demo",
    )
    assert state.stage == 1

    status = api.migrate_status("demo.Counts", app_name="demo")
    assert status is not None
    assert status.stage == 1
    assert status.source_backend == "elasticache-redis"
    assert status.target_backend == "dynamodb"


def test_migrate_start_rejects_duplicate(migration_sandbox: Path) -> None:
    """Starting twice in a row raises RuntimeError."""
    api.migrate_start("demo.X", "a", "b", app_name="demo")
    with pytest.raises(RuntimeError, match="already in progress"):
        api.migrate_start("demo.X", "a", "b", app_name="demo")


def test_migrate_advance_and_rollback(migration_sandbox: Path) -> None:
    """advance() increments the stage; rollback() decrements it."""
    api.migrate_start("demo.X", "a", "b", app_name="demo")
    state = api.migrate_advance("demo.X", app_name="demo")
    assert state.stage == 2

    state = api.migrate_rollback("demo.X", app_name="demo")
    assert state.stage == 1


def test_migrate_advance_without_start(migration_sandbox: Path) -> None:
    """Advancing a migration that was never started raises RuntimeError."""
    with pytest.raises(RuntimeError):
        api.migrate_advance("demo.X", app_name="demo")


def test_migrate_list_empty(migration_sandbox: Path) -> None:
    """An empty migration directory yields an empty list."""
    assert api.migrate_list() == []


def test_migrate_list_global(migration_sandbox: Path) -> None:
    """migrate_list() with no app_name scans every sub-directory."""
    api.migrate_start("a.X", "x", "y", app_name="one")
    api.migrate_start("b.Y", "x", "y", app_name="two")

    states = api.migrate_list()
    variables = {s.variable_name for s in states}
    assert variables == {"a.X", "b.Y"}


def test_infra_cleanup_removes_state(migration_sandbox: Path) -> None:
    """infra_cleanup() removes an in-progress migration state file."""
    api.migrate_start("demo.X", "a", "b", app_name="demo")
    assert api.infra_cleanup("demo.X", app_name="demo") is True
    assert api.migrate_status("demo.X", app_name="demo") is None


def test_infra_cleanup_missing_state(migration_sandbox: Path) -> None:
    """infra_cleanup() returns False when no state file exists."""
    assert api.infra_cleanup("nobody.home", app_name="demo") is False


# ── run / build_runtime ───────────────────────────────────────────────────────


def test_build_runtime_returns_local_runtime(simple_app: App) -> None:
    """build_runtime() returns a LocalRuntime with the app attached."""
    from skaal.runtime.local import LocalRuntime

    runtime = api.build_runtime(simple_app, host="127.0.0.1", port=9999)
    assert isinstance(runtime, LocalRuntime)
    assert runtime.app is simple_app
    assert runtime.host == "127.0.0.1"
    assert runtime.port == 9999


def test_build_runtime_from_plan_uses_local_fallbacks(simple_app: App) -> None:
    from skaal.backends.sqlite_backend import SqliteBackend
    from skaal.plan import PlanFile, StorageSpec
    from skaal.runtime.local import LocalRuntime
    from skaal.storage import Store

    planned_app = App(name="planned-app")

    @planned_app.storage
    class Counter(Store[int]):
        pass

    plan_file = PlanFile(
        app_name="planned-app",
        deploy_target="aws",
        storage={
            "planned-app.Counter": StorageSpec(
                variable_name="planned-app.Counter",
                backend="dynamodb",
                kind="kv",
            )
        },
    )

    runtime = api.build_runtime(planned_app, plan=plan_file)
    assert isinstance(runtime, LocalRuntime)
    assert isinstance(runtime._backends["planned-app.Counter"], SqliteBackend)


def test_build_runtime_rejects_plan_with_runtime_shortcuts(simple_app: App) -> None:
    from skaal.plan import PlanFile

    with pytest.raises(ValueError, match="plan cannot be combined"):
        api.build_runtime(simple_app, plan=PlanFile(app_name="test-app"), persist=True)


def test_run_invokes_serve(simple_app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    """run() constructs a runtime and awaits its serve() method."""
    called: dict[str, bool] = {"serve": False}

    async def _fake_serve() -> None:
        called["serve"] = True

    def _fake_build_runtime(*args, **kwargs):
        class _FakeRuntime:
            async def serve(self):
                await _fake_serve()

        return _FakeRuntime()

    monkeypatch.setattr(api, "build_runtime", _fake_build_runtime)
    api.run(simple_app)
    assert called["serve"] is True


# ── deploy ────────────────────────────────────────────────────────────────────


def test_deploy_raises_when_dir_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """deploy() raises FileNotFoundError if artifacts_dir is absent."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        api.deploy("artifacts_does_not_exist")


def test_deploy_forwards_to_deploy_artifacts(tmp_path: Path) -> None:
    """deploy() resolves settings and delegates to deploy_artifacts()."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    with mock.patch("skaal.deploy.deploy_artifacts") as fake:
        fake.return_value = {"apiUrl": "https://example.com"}
        result = api.deploy(
            artifacts,
            stack="dev",
            region="us-east-1",
            gcp_project=None,
            yes=True,
        )

    assert result == {"apiUrl": "https://example.com"}
    fake.assert_called_once()
    call_kwargs = fake.call_args.kwargs
    assert call_kwargs["stack"] == "dev"
    assert call_kwargs["region"] == "us-east-1"
    assert call_kwargs["runtime_options"] == {"detach": False, "follow_logs": False}


def test_deploy_forwards_local_runtime_options(tmp_path: Path) -> None:
    """api.deploy() should forward local runtime flags to the deploy target layer."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    with mock.patch("skaal.deploy.deploy_artifacts") as fake:
        fake.return_value = {}
        api.deploy(artifacts, local_detach=True, local_follow_logs=True)

    call_kwargs = fake.call_args.kwargs
    assert call_kwargs["runtime_options"] == {"detach": True, "follow_logs": True}


def test_deploy_pre_hook_failure_skips_deploy_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing pre-deploy hook aborts before the deploy executor runs."""
    from skaal.deploy.push import write_meta
    from skaal.errors import SkaalHookError

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    write_meta(artifacts, target="aws", source_module="examples.counter", app_name="demo")

    class _Settings:
        stack = "dev"
        region = "us-east-1"
        gcp_project = None
        overrides: dict[str, str] = {}
        deletion_protection = None
        pre_deploy = [["missing-pre-hook"]]
        post_deploy: list[list[str]] = []
        stacks: dict[str, object] = {}

        def for_stack(self, name: str | None = None):
            self.stack = name or self.stack
            return self

    monkeypatch.setattr(api, "SkaalSettings", lambda: _Settings())

    with mock.patch("skaal.deploy.deploy_artifacts") as fake_push:
        with pytest.raises(SkaalHookError) as exc_info:
            api.deploy(artifacts)

    fake_push.assert_not_called()
    assert "no infrastructure changes were applied" in str(exc_info.value)


def test_deploy_post_hook_failure_reports_committed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing post-deploy hook should explain that infra deploy already completed."""
    from skaal.deploy.push import write_meta
    from skaal.errors import SkaalHookError

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    write_meta(artifacts, target="aws", source_module="examples.counter", app_name="demo")

    class _Settings:
        stack = "dev"
        region = "us-east-1"
        gcp_project = None
        overrides: dict[str, str] = {}
        deletion_protection = None
        pre_deploy: list[list[str]] = []
        post_deploy = [["missing-post-hook"]]
        stacks: dict[str, object] = {}

        def for_stack(self, name: str | None = None):
            self.stack = name or self.stack
            return self

    monkeypatch.setattr(api, "SkaalSettings", lambda: _Settings())

    with mock.patch("skaal.deploy.deploy_artifacts", return_value={"apiUrl": "https://x"}):
        with pytest.raises(SkaalHookError) as exc_info:
            api.deploy(artifacts)

    message = str(exc_info.value)
    assert "Post-deploy hook failed." in message
    assert "already completed" in message
    assert "SKAAL_OUTPUT_*" in message
