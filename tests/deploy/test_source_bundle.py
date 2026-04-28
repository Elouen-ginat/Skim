from __future__ import annotations

from skaal.app import App
from skaal.deploy.packaging.source_bundle import copy_source_package
from skaal.deploy.runtime_assets import resolve_bootstrap_artifact
from skaal.deploy.targets.local_compose import LocalComposeBuilder
from skaal.plan import PlanFile


def test_copy_source_package_copies_root_module_file(tmp_path) -> None:
    project_root = tmp_path / "project"
    artifacts_dir = project_root / "artifacts"
    project_root.mkdir()
    artifacts_dir.mkdir()
    (project_root / "main.py").write_text("app = object()\n", encoding="utf-8")

    copied = copy_source_package(
        artifacts_dir,
        project_root=project_root,
        source_module="main",
    )

    assert copied == artifacts_dir / "main.py"
    assert copied.read_text(encoding="utf-8") == "app = object()\n"


def test_resolve_bootstrap_artifact_renames_conflicting_root_main() -> None:
    bootstrap = resolve_bootstrap_artifact("main", default_filename="main.py")

    assert bootstrap.filename == "_skaal_bootstrap.py"
    assert bootstrap.module_name == "_skaal_bootstrap"


def test_resolve_bootstrap_artifact_keeps_package_entrypoint_names() -> None:
    bootstrap = resolve_bootstrap_artifact("myapp.main", default_filename="main.py")

    assert bootstrap.filename == "main.py"
    assert bootstrap.module_name == "main"


def test_local_builder_keeps_root_main_and_uses_renamed_bootstrap(tmp_path, monkeypatch) -> None:
    project_root = tmp_path / "project"
    artifacts_dir = project_root / "artifacts"
    project_root.mkdir()
    monkeypatch.syspath_prepend(str(project_root))

    (project_root / "main.py").write_text(
        'from skaal.app import App\napp = App(name="demo")\n',
        encoding="utf-8",
    )

    generated = LocalComposeBuilder().build(
        App(name="demo"),
        PlanFile(app_name="demo", deploy_target="local"),
        artifacts_dir,
        source_module="main",
    )

    generated_names = {path.name for path in generated}
    assert "main.py" in generated_names
    assert "_skaal_bootstrap.py" in generated_names
    assert "--reload _skaal_bootstrap:application" in (
        artifacts_dir / "docker-compose.yml"
    ).read_text(encoding="utf-8")
