"""`skaal catalog` — browse available infrastructure backends."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(help="Browse available infrastructure backends.")


@app.callback(invoke_without_command=True)
def catalog(
    catalog_path: Optional[Path] = typer.Option(
        None, "--catalog", help="Path to catalog TOML. Defaults to catalogs/aws.toml."
    ),
    section: str = typer.Option(
        "all",
        "--section",
        "-s",
        help="Section to display: all, storage, compute, network.",
    ),
) -> None:
    """Show all available backends from the infrastructure catalog."""
    from skaal.catalog.loader import load_typed_catalog

    try:
        cat = load_typed_catalog(catalog_path)
    except FileNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    if section in ("all", "storage") and cat.storage:
        typer.echo(f"Storage backends ({len(cat.storage)}):")
        typer.echo(
            f"  {'Name':<28} {'Display':<32} {'Read lat':<12} {'Durability':<22} {'$/GB/mo'}"
        )
        typer.echo(f"  {'-'*28} {'-'*32} {'-'*12} {'-'*22} {'-'*8}")
        for name, spec in sorted(cat.storage.items()):
            dur = ", ".join(spec.durability)
            lat = f"{spec.read_latency.min}–{spec.read_latency.max}ms"
            typer.echo(
                f"  {name:<28} {spec.display_name:<32} {lat:<12} {dur:<22} ${spec.cost_per_gb_month:.3f}"
            )
        typer.echo("")

    if section in ("all", "compute") and cat.compute:
        typer.echo(f"Compute backends ({len(cat.compute)}):")
        typer.echo(f"  {'Name':<28} {'Display':<32} {'vCPUs':<8} {'Memory':<10} {'$/hr'}")
        typer.echo(f"  {'-'*28} {'-'*32} {'-'*8} {'-'*10} {'-'*8}")
        for name, cspec in sorted(cat.compute.items()):
            typer.echo(
                f"  {name:<28} {cspec.display_name:<32} {cspec.vcpus:<8} {cspec.memory_gb:<10.1f} ${cspec.cost_per_hour:.4f}"
            )
        typer.echo("")

    if section in ("all", "network") and cat.network:
        typer.echo(f"Network backends ({len(cat.network)}):")
        for name, nspec in sorted(cat.network.items()):
            typer.echo(f"  {name}: {nspec.display_name}")
        typer.echo("")

    if not cat.storage and not cat.compute and not cat.network:
        typer.echo("Catalog is empty.")
