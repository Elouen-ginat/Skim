# ADR 004 — Six-Stage Backend Migration Protocol

**Status:** Accepted
**Date:** 2024-01-01

## Context

When the solver selects a different backend for a storage resource (e.g.
Redis → DynamoDB), migrating live data is risky.  We need a safe, incremental
protocol that avoids data loss and allows rollback.

## Decision

Adopt a **six-stage migration protocol**:

| Stage | Name            | Reads from   | Writes to       |
|-------|-----------------|--------------|-----------------|
| 0     | `none`          | old          | old             |
| 1     | `shadow_write`  | old          | old + new       |
| 2     | `shadow_read`   | old (verify) | old + new       |
| 3     | `dual_read`     | new (primary)| old + new       |
| 4     | `new_primary`   | new          | new only        |
| 5     | `cleanup`       | new          | new (old drain) |
| 6     | `done`          | new          | new             |

Each stage is stored in `plan.skaal.lock` as `migration_stage`.  Operators
advance stages via `skaal migrate --stage <n>`.

## Consequences

**Positive:**
- Zero-downtime migrations with incremental verification.
- Rollback is possible at any stage before `new_primary`.
- Stage is durable in the lock file, survives restarts.

**Negative:**
- Dual-write increases write latency during migration.
- Operators must manually advance stages (by design — safety gate).
