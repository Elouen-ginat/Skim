# ADR 003 — TOML Catalog Format

**Status:** Accepted  
**Date:** 2024-01-01

## Context

The solver needs a machine-readable description of available backends and their
capabilities.  We need a format that is human-editable, version-controllable,
and easy to parse in Python.

## Decision

Use **TOML** for catalog files, stored under `catalogs/`.

Each catalog file represents a target environment (e.g. `aws.toml`, `gcp.toml`,
`local.toml`).  Top-level sections are `[storage.*]`, `[compute.*]`, and
`[network.*]`.

```toml
[storage.elasticache-redis]
display_name = "Amazon ElastiCache (Redis)"
read_latency  = { min = 0.1, max = 2.0, unit = "ms" }
durability    = ["ephemeral", "persistent"]
cost_per_gb_month = 3.50
```

Python 3.11+ `tomllib` (stdlib) reads the files; no external TOML library
needed for production.

## Consequences

**Positive:**
- TOML is human-friendly and well-understood.
- `tomllib` is stdlib in Python 3.11+.
- Adding a new backend requires only a TOML stanza, no code changes.

**Negative:**
- Catalog values are approximate; real-world performance varies.
- Multi-cloud catalogs must be maintained separately.
