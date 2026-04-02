# ADR 002 — Z3 Solver for Backend Selection

**Status:** Accepted  
**Date:** 2024-01-01

## Context

Given a set of constraints and a catalog of backends, we need a principled way
to select the best backend.  Ad-hoc if/else logic would be fragile and hard to
extend.

## Decision

Use the **Z3 SMT solver** (`z3-solver` Python package) to encode backend
selection as a constraint satisfaction / optimization problem.

Each backend is a Boolean decision variable.  Hard constraints (durability,
access_pattern, latency bounds) eliminate incompatible backends.  A cost
minimization objective selects the cheapest surviving candidate.

The solver runs at `skaal plan` time, not at runtime.

## Consequences

**Positive:**
- Correct by construction: selected backend provably satisfies all constraints.
- Easy to add new constraint types by extending the Z3 encoding.
- Decisions are reproducible given the same catalog.

**Negative:**
- `z3-solver` is a heavyweight dependency (~50 MB).
- Solver overhead is acceptable for planning but not for hot paths.
- Encoding must be kept in sync with catalog schema.
