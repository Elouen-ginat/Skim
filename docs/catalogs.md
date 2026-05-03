# Catalogs — overrides, validation, and provenance

A Skaal catalog is a TOML file that lists the storage / compute / network
backends the solver can pick from. Most projects start with a single
`catalogs/local.toml` (or `aws.toml` / `gcp.toml`) and never look at it
again. Once you have more than one environment, the catalog becomes
something you maintain — and ADR 022 adds the two surfaces that make
multi-environment catalogs ergonomic:

1. **`[skaal] extends`** — overlay one catalog on top of another instead of
   copy-pasting the whole file.
2. **`skaal catalog validate` / `skaal catalog sources`** — surface the
   typed validators (already enforced when `skaal plan` runs) and the
   resolved override chain at any time.

## Inheritance

Every catalog file may declare a parent under a reserved `[skaal]` table:

```toml
# catalogs/dev.toml
[skaal]
extends = "base.toml"          # relative path, anchored at this file's dir

[storage.sqlite]
display_name  = "SQLite (dev — looser latency)"
read_latency  = { min = 0.1, max = 50.0, unit = "ms" }
write_latency = { min = 0.5, max = 100.0, unit = "ms" }
durability    = ["persistent"]
storage_kinds = ["kv"]
access_patterns = ["random-read", "random-write"]
cost_per_gb_month = 0.0
```

The merged result is what the solver sees. Resolution rules:

- **Per-backend replacement.** If a child declares `[storage.sqlite]`, it
  replaces the parent's whole `[storage.sqlite]` block. Field-level
  merging is intentionally not supported — "swap a backend, add a
  backend" is the user-facing model.
- **Sections passthrough.** Unknown top-level tables are passed through
  with the child winning.
- **Chains.** A parent may itself `extends` another file. Cycles raise
  `CatalogError`.
- **Short names.** `extends = "aws"` resolves through the bundled
  catalogs (`skaal/catalog/data/aws.toml`) and any `skaal.catalogs`
  entry-point a plugin registered.

To prune a backend the parent declared without redeclaring it, use the
`[skaal] remove` list:

```toml
[skaal]
extends = "base.toml"
remove  = ["storage.redis", "compute.t3-micro"]
```

`remove` paths are dotted `section.name`. Removing an absent entry logs
a warning but does not fail the load — parents move; this is a soft
contract.

The reserved `[skaal]` table is stripped before the merged dict reaches
any downstream consumer (`skaal plan`, the typed Pydantic
`Catalog.from_raw`, or your own scripts using `load_catalog(...)`).

## Validating a catalog

`skaal catalog validate <path>` runs the same Pydantic validators
`skaal plan` would run, without spinning up the solver:

```
$ skaal catalog validate catalogs/dev.toml
OK /abs/path/catalogs/dev.toml — 2 storage, 0 compute, 0 network backends
```

On failure, the exit code is **2** (matching the solver-UNSAT exit code
introduced in ADR 021) so CI can tell "Skaal rejected your catalog" from
"Skaal crashed":

```
$ skaal catalog validate catalogs/bad.toml
Catalog validation FAILED: 1 validation error for StorageBackendSpec
write_latency
  Field required [type=missing, …]
$ echo $?
2
```

## Inspecting the resolved chain

`skaal catalog sources <path>` prints the resolved chain root → leaf, with
any `remove` entries each layer contributes:

```
$ skaal catalog sources catalogs/dev.toml
/abs/path/catalogs/base.toml
  └─ /abs/path/catalogs/dev.toml
        removes: storage.redis
```

This is the answer to "where did this `read_latency` come from?" — handy
when an `extends` chain stops behaving the way you expected.

## Common patterns

### `base.toml` + `dev.toml` + `prod.toml`

```
catalogs/
  base.toml          # backends every environment can pick from
  dev.toml           # extends = "base.toml"; relax latency, swap to sqlite
  prod.toml          # extends = "base.toml"; tighten durability, add region
```

Run with:

```
skaal plan --catalog catalogs/dev.toml  --target aws
skaal plan --catalog catalogs/prod.toml --target aws
```

### Extending a bundled catalog

```toml
# catalogs/local.toml
[skaal]
extends = "local"     # bundled catalog — skaal/catalog/data/local.toml

[storage.local-redis]
display_name = "Redis (with TLS)"
…
```

If you do this, **pin your Skaal version** so the bundled parent does not
shift under you between releases. `skaal catalog sources` makes drift
visible.

## Programmatic API

```python
from skaal.catalog.loader import load_catalog, load_catalog_with_sources
from skaal.types import CatalogSource

# Merged dict, ready for Catalog.from_raw or direct consumption.
merged: dict = load_catalog("catalogs/dev.toml")

# Resolved chain — useful for diagnostics or custom rendering.
source: CatalogSource = load_catalog_with_sources("catalogs/dev.toml")
for node in source.chain():
    print(node.path, node.removes)
```

`CatalogSource` is exported from `skaal.types`.
