"""`skaal init` — scaffold a new Skaal project."""

from __future__ import annotations

import logging
from importlib.resources import files
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel

from skaal.cli._errors import cli_error_boundary

app = typer.Typer(
    help="Scaffold a new Skaal project.",
    context_settings={"allow_interspersed_args": True},
)
log = logging.getLogger("skaal.cli")

_TEMPLATES = "skaal.cli.templates.init"
_BUNDLED_CATALOG = "skaal.catalog.data"

# Template filename → output path (relative to the project root).
# ``{name}`` in the path is substituted with the project name.
_LAYOUT: dict[str, str] = {
    "pyproject.toml.tmpl": "pyproject.toml",
    "app.py.tmpl": "{name}/app.py",
    "gitignore.tmpl": ".gitignore",
    "README.md.tmpl": "README.md",
}


@app.callback(invoke_without_command=True)
@cli_error_boundary
def init(
    name: Optional[str] = typer.Argument(
        None,
        help=(
            "Project name. Must be a valid Python identifier. "
            "Defaults to the current directory's name when --here is set."
        ),
    ),
    here: bool = typer.Option(
        False, "--here", help="Scaffold into the current directory instead of ./<name>."
    ),
    force: bool = typer.Option(
        False, "--force", "-f", help="Overwrite existing files."
    ),
) -> None:
    """Create a starter Skaal project at ``./<name>`` (or in cwd with ``--here``)."""
    resolved = name or (Path.cwd().name if here else None)
    if resolved is None:
        raise ValueError("missing project name (or pass --here to use the current directory).")
    if not resolved.isidentifier():
        raise ValueError(
            f"'{resolved}' is not a valid Python identifier (use letters, digits, _)."
        )

    root = Path.cwd() if here else Path.cwd() / resolved
    root.mkdir(parents=True, exist_ok=True)
    name = resolved

    written = _render_layout(root, name, force=force)
    written.append(_write_catalog(root, force=force))
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "__init__.py").touch(exist_ok=True)

    Console().print(
        Panel.fit(
            f"Scaffolded [bold]{name}[/bold] in [cyan]{root}[/cyan]\n\n"
            f"  cd {root.name if not here else '.'}\n"
            f"  pip install -e .\n"
            f"  skaal run",
            title="next steps",
        )
    )
    for path in written:
        log.info("  wrote %s", path.relative_to(root))


def _render_layout(root: Path, name: str, *, force: bool) -> list[Path]:
    written: list[Path] = []
    template_pkg = files(_TEMPLATES)
    for tmpl_name, target_pattern in _LAYOUT.items():
        target = root / target_pattern.format(name=name)
        if target.exists() and not force:
            raise FileExistsError(f"refusing to overwrite {target} (pass --force).")
        target.parent.mkdir(parents=True, exist_ok=True)
        body = template_pkg.joinpath(tmpl_name).read_text(encoding="utf-8")
        target.write_text(body.format(name=name), encoding="utf-8")
        written.append(target)
    return written


def _write_catalog(root: Path, *, force: bool) -> Path:
    target = root / "catalogs" / "local.toml"
    if target.exists() and not force:
        raise FileExistsError(f"refusing to overwrite {target} (pass --force).")
    target.parent.mkdir(parents=True, exist_ok=True)
    body = files(_BUNDLED_CATALOG).joinpath("local.toml").read_text(encoding="utf-8")
    target.write_text(body, encoding="utf-8")
    return target
