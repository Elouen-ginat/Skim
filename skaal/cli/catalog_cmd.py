"""`skaal catalog` — browse, validate, and trace infrastructure catalogs.

Sub-commands (ADR 022):

* ``skaal catalog``                 — print all backends from the resolved catalog.
* ``skaal catalog validate <path>`` — run the typed validators (storage / compute /
  network deploy + wire blocks) and exit non-zero on failure.
* ``skaal catalog sources <path>``  — print the resolved ``[skaal] extends`` chain
  so users can see which file contributed each layer.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import typer

from skaal.cli._errors import cli_error_boundary
from skaal.errors import CatalogError
from skaal.types.catalog import CatalogSource

app = typer.Typer(help="Browse, validate, and trace infrastructure catalogs.")
log = logging.getLogger("skaal.cli")


# ── default command: browse ───────────────────────────────────────────────────


@app.callback(invoke_without_command=True)
def _root(ctx: typer.Context) -> None:
    """Run the browse view when no subcommand is given."""
    if ctx.invoked_subcommand is None:
        _browse(catalog_path=None, section="all")


def _browse(catalog_path: Optional[Path], section: str) -> None:
    """Print the catalog's backend tables.  Unchanged behaviour pre-ADR-022."""
    from skaal import api

    cat = api.catalog(catalog_path)

    if section in ("all", "storage") and cat.storage:
        log.info("Storage backends (%s):", len(cat.storage))
        log.info(f"  {'Name':<28} {'Display':<32} {'Read lat':<12} {'Durability':<22} {'$/GB/mo'}")
        log.info(f"  {'-' * 28} {'-' * 32} {'-' * 12} {'-' * 22} {'-' * 8}")
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
        log.info(f"  {'-' * 28} {'-' * 32} {'-' * 8} {'-' * 10} {'-' * 8}")
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


@app.command("browse")
@cli_error_boundary
def browse(
    catalog_path: Optional[Path] = typer.Option(
        None, "--catalog", help="Path to catalog TOML or registered short name."
    ),
    section: str = typer.Option(
        "all",
        "--section",
        "-s",
        help="Section to display: all, storage, compute, network.",
    ),
) -> None:
    """Show all backends in the resolved catalog."""
    _browse(catalog_path, section)


# ── validate ──────────────────────────────────────────────────────────────────


@app.command("validate")
@cli_error_boundary
def validate(
    catalog_path: Optional[Path] = typer.Argument(
        None,
        help="Path to catalog TOML (or registered short name). Falls back to discovery.",
    ),
) -> None:
    """Run the typed catalog validators against ``PATH`` and report.

    Exits with code 2 on validation failure (consistent with the
    solver-UNSAT exit code introduced in ADR 021).
    """
    from skaal.catalog.loader import load_catalog, load_catalog_with_sources
    from skaal.catalog.models import Catalog

    source = load_catalog_with_sources(catalog_path)
    merged = load_catalog(catalog_path)

    try:
        cat = Catalog.from_raw(merged)
    except (ValueError, CatalogError) as exc:
        log.error("Catalog validation FAILED: %s", exc)
        raise typer.Exit(2) from exc

    label = str(source.path) if source.path is not None else "<bundled>"
    log.info(
        "OK %s — %d storage, %d compute, %d network backends",
        label,
        len(cat.storage),
        len(cat.compute),
        len(cat.network),
    )


# ── sources ───────────────────────────────────────────────────────────────────


@app.command("sources")
@cli_error_boundary
def sources(
    catalog_path: Optional[Path] = typer.Argument(
        None,
        help="Path to catalog TOML (or registered short name). Falls back to discovery.",
    ),
) -> None:
    """Print the resolved ``[skaal] extends`` chain for *PATH*."""
    from skaal.catalog.loader import load_catalog_with_sources

    source = load_catalog_with_sources(catalog_path)
    _print_chain(source)


def _print_chain(leaf: CatalogSource) -> None:
    """Render a chain root-first as ``parent → child → leaf``."""
    chain = leaf.chain()
    for depth, node in enumerate(chain):
        prefix = ("  " * depth) + ("└─ " if depth else "")
        label = str(node.path) if node.path is not None else "<bundled>"
        log.info("%s%s", prefix, label)
        if node.removes:
            log.info("%s   removes: %s", "  " * depth, ", ".join(node.removes))
