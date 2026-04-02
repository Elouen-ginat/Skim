# ADR 001 — Infrastructure as Constraints

**Status:** Accepted  
**Date:** 2024-01-01

## Context

Traditional infrastructure-as-code (IaC) tools (Terraform, Pulumi, CDK) require
developers to specify *what* infrastructure to provision.  This couples application
code to infrastructure decisions and forces application engineers to become
infrastructure experts.

## Decision

Skaal adopts an **Infrastructure as Constraints** model: developers declare *what
properties the infrastructure must satisfy* (latency, durability, throughput,
access pattern), and a Z3 constraint solver selects the concrete backend.

```python
@app.storage(read_latency="< 5ms", durability="persistent")
class Sessions:
    pass
```

The Z3 solver reads a TOML catalog of backend specifications and selects the
cheapest backend that satisfies every constraint.

## Consequences

**Positive:**
- Application code is decoupled from backend technology.
- Infrastructure decisions are auditable, reproducible, and explained.
- Backend can be swapped without touching application code.

**Negative:**
- Catalog must be kept up to date with real backend capabilities.
- Solver decisions may surprise developers unfamiliar with the catalog.
- Complex multi-constraint scenarios may require catalog tuning.
