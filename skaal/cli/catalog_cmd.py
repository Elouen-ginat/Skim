"""`skaal catalog` — browse available infrastructure backends."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import typer

from skaal.cli._errors import cli_error_boundary

app = typer.Typer(help="Browse available infrastructure backends.")
log = logging.getLogger("skaal.cli")


@app.callback(invoke_without_command=True)
@cli_error_boundary
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
    from skaal import api

    cat = api.catalog(catalog_path)

    if section in ("all", "storage") and cat.storage:
        log.info("Storage backends (%s):", len(cat.storage))
        log.info(
            f"  {'Name':<28} {'Display':<32} {'Read lat':<12} " f"{'Durability':<22} {'$/GB/mo'}"
        )
        log.info(f"  {'-'*28} {'-'*32} {'-'*12} {'-'*22} {'-'*8}")
        for name, spec in sorted(cat.storage.items()):
            dur = ", ".join(spec.durability)
            lat = f"{spec.read_latency.min}–{spec.read_latency.max}ms"
            log.info(
                f"  {name:<28} {spec.display_name:<32} {lat:<12} "
                f"{dur:<22} ${spec.cost_per_gb_month:.3f}"
            )
        log.info("")

    if section in ("all", "compute") and cat.compute:
        log.info("Compute backends (%s):", len(cat.compute))
        log.info(f"  {'Name':<28} {'Display':<32} {'vCPUs':<8} {'Memory':<10} {'$/hr'}")
        log.info(f"  {'-'*28} {'-'*32} {'-'*8} {'-'*10} {'-'*8}")
        for name, cspec in sorted(cat.compute.items()):
            log.info(
                f"  {name:<28} {cspec.display_name:<32} {cspec.vcpus:<8} "
                f"{cspec.memory_gb:<10.1f} ${cspec.cost_per_hour:.4f}"
            )
        log.info("")

    if section in ("all", "network") and cat.network:
        log.info("Network backends (%s):", len(cat.network))
        for name, nspec in sorted(cat.network.items()):
            log.info("  %s: %s", name, nspec.display_name)
        log.info("")

    if not cat.storage and not cat.compute and not cat.network:
        log.info("Catalog is empty.")
