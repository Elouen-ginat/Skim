from __future__ import annotations

import shutil
from pathlib import Path


def copy_source_package(output_dir: Path, *, project_root: Path, source_module: str) -> Path | None:
    top_pkg = source_module.split(".")[0]
    src_pkg_dir = project_root / top_pkg
    dst_pkg_dir = output_dir / top_pkg
    if not src_pkg_dir.is_dir():
        return None
    shutil.copytree(src_pkg_dir, dst_pkg_dir, dirs_exist_ok=True)
    return dst_pkg_dir


def copy_mesh_bundle(output_dir: Path, *, project_root: Path) -> Path | None:
    mesh_src_dir = project_root / "mesh"
    if not (mesh_src_dir.is_dir() and (mesh_src_dir / "Cargo.toml").exists()):
        return None
    mesh_bundle_dir = output_dir / "mesh"
    shutil.copytree(mesh_src_dir, mesh_bundle_dir, dirs_exist_ok=True)
    return mesh_bundle_dir


def copy_dev_skaal_bundle(output_dir: Path, *, project_root: Path) -> Path | None:
    skaal_src_dir = project_root / "skaal"
    skaal_root_pyproject = project_root / "pyproject.toml"
    if not (skaal_src_dir.is_dir() and skaal_root_pyproject.exists()):
        return None

    skaal_bundle_dir = output_dir / "_skaal"
    skaal_bundle_dir.mkdir(exist_ok=True)
    shutil.copytree(skaal_src_dir, skaal_bundle_dir / "skaal", dirs_exist_ok=True)

    raw = skaal_root_pyproject.read_text(encoding="utf-8")
    raw = raw.replace('path = "mesh"', 'path = "../mesh"')
    (skaal_bundle_dir / "pyproject.toml").write_text(raw, encoding="utf-8")

    for extra in ("LICENSE", "README.md"):
        src = project_root / extra
        if src.exists():
            shutil.copy2(src, skaal_bundle_dir / extra)

    return skaal_bundle_dir
