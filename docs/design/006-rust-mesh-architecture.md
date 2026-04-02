# ADR 006 — Rust Mesh Architecture

**Status:** Proposed  
**Date:** 2024-01-01

## Context

Production workloads require high-throughput, low-latency function dispatch,
efficient sidecar proxying, and distributed state management.  Python asyncio
is suitable for I/O-bound work but not for the mesh control plane.

## Decision

Implement the **Skaal Mesh** in Rust (`mesh/` workspace member):

- **gRPC server** — accepts function invocations from the Python runtime.
- **Sidecar proxy** — intercepts traffic between functions for observability.
- **Distributed state** — pluggable backends (Redis, etcd) via Rust traits.
- **Agent registry** — tracks running function instances across the cluster.

The Python `skaal.runtime.distributed.DistributedRuntime` communicates with
the mesh over gRPC.  In local mode, it falls back to direct Python calls.

The Rust crate is built via `maturin` and exposed as a Python extension module
for tight integration.

## Consequences

**Positive:**
- Sub-millisecond dispatch overhead in production.
- Memory-safe, fearless concurrency for the control plane.
- Rust and Python share the same package (maturin builds both).

**Negative:**
- Rust build dependency increases CI time.
- Developers need Rust toolchain installed for mesh development.
- gRPC schema must be kept in sync with Python stubs.
