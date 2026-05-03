# ADR 016 — Blob Storage Tier Implementation Plan

**Status:** Implemented
**Date:** 2026-04-30
**Related:** [user_gaps.md §B.2](../user_gaps.md), [ADR 015](015-store-surface-implementation-plan.md)

## Goal

Land the next post-ADR-015 implementation pass on a first-class blob/object storage tier.

This pass should solve the highest-ranked remaining storage P0 from the user-gap review:

1. there is no `@app.blob` surface
2. there is no local filesystem blob backend
3. there is no S3 or GCS blob backend or deploy wiring

Right now, the moment a user needs avatars, attachments, exports, model artifacts, or any other file payload, they leave Skaal's storage surface and wire a second stack by hand. That is the next broad, user-visible capability hole after ADR 015.

## Why this is next

ADR 015 fixed the most obvious `Store[T]` expressiveness gap: users can now page and query declared indexes. That moved the storage story from "toy CRUD only" to "basic app data works".

The next immediate reason users outgrow the framework is files:

- profile photos
- document attachments
- generated reports
- dataset imports and exports
- model checkpoints and prompt artifacts

These are not edge cases. They are standard application requirements, and Skaal currently has no first-class answer for them.

This is also a coherent cut:

- the planner already understands storage `kind`
- runtime wiring already switches on `__skaal_storage__`
- the backend registry already supports adding new built-in backends
- the AWS and GCP extras already include the cloud SDKs needed for S3 and GCS

That means the missing work is a focused product surface and wiring pass, not a research project.

## Scope

This pass includes:

- a public `@app.blob(...)` decorator
- a `BlobStore` user-facing API for object reads, writes, deletes, stats, and listing
- typed blob metadata objects
- a local filesystem backend for development and tests
- S3 and GCS backends
- solver, catalog, runtime, and deploy support for `kind="blob"`
- docs, one example, and backend contract tests

This pass does **not** include:

- presigned URLs
- multipart or resumable uploads
- CDN integration or public asset hosting
- image or video transforms
- bucket lifecycle/versioning policy management
- virus scanning or content moderation hooks

Those are all valid follow-on features, but they should not block the first coherent blob tier.

## Current facts

Today:

- `skaal/decorators.py` exposes `storage`, `relational`, and `vector`, but there is no blob decorator.
- `skaal/module.py` exposes `Module.storage`, `Module.relational`, and `Module.vector`, but no blob registration surface.
- `skaal/runtime/local.py` wires `Store`, relational models, and vector stores, but there is no blob runtime path.
- `pyproject.toml` registers built-in backends under `project.entry-points."skaal.backends"`, but none target object storage.
- the AWS extra already ships `boto3`, and the GCP extra already ships `google-cloud-storage`.

That keeps the work localized to the existing storage architecture rather than forcing a parallel system.

## Decision

Add a separate `BlobStore` tier that flows through the existing storage-planning pipeline via `kind="blob"`.

Do **not** try to fake object storage as `Store[bytes]`.

`Store[T]` is a KV abstraction with value-centric operations. Blob storage has different semantics:

- large opaque payloads
- file-path upload and download helpers
- content type and metadata
- bucket/container provisioning
- key listing over object namespaces rather than row traversal

Trying to stretch `Store[bytes]` into this would hide the real deployment and runtime differences instead of modeling them.

## Public API to add

### 1. Blob metadata type

Add `skaal/types/blob.py` with a single metadata object and re-export it from `skaal.types` and top-level `skaal`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class BlobObject:
    key: str
    size: int
    content_type: str | None = None
    etag: str | None = None
    updated_at: datetime | None = None
    metadata: dict[str, str] = field(default_factory=dict)
```

Use the existing `Page[T]` type from ADR 015 for paged listings.

### 2. Blob store base class

Add `skaal/blob.py` with a `BlobStore` base class, parallel to `skaal/storage.py` and `Store[T]`:

```python
class BlobStore:
    @classmethod
    async def put_bytes(
        cls,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> BlobObject: ...

    @classmethod
    async def put_file(
        cls,
        key: str,
        source: str | Path,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> BlobObject: ...

    @classmethod
    async def get_bytes(cls, key: str) -> bytes: ...

    @classmethod
    async def download_file(cls, key: str, destination: str | Path) -> Path: ...

    @classmethod
    async def stat(cls, key: str) -> BlobObject | None: ...

    @classmethod
    async def exists(cls, key: str) -> bool: ...

    @classmethod
    async def delete(cls, key: str) -> None: ...

    @classmethod
    async def list_page(
        cls,
        prefix: str = "",
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> Page[BlobObject]: ...
```

Also add sync wrappers using `skaal.sync.run(...)`, matching the pattern already used by `Store[T]`.

### 3. Decorator surface

Add a new decorator:

```python
@app.blob(
    read_latency="< 50ms",
    durability="persistent",
    size_hint="500",
    residency="eu-west-1",
)
class Uploads(BlobStore):
    pass
```

This should reuse the existing storage constraint vocabulary and set:

```python
__skaal_storage__["kind"] = "blob"
```

Do not add a second constraint language just for blobs.

### 4. Module and App registration

Add `Module.blob(...)` and `App.blob(...)` as first-class registration helpers.

Implementation rule: keep blob resources inside the existing storage registration/export path rather than inventing a parallel `_blobs` registry. Blob is a storage kind, not a separate top-level resource family.

## Backend contract changes

Add a `BlobBackend` protocol to `skaal/backends/base.py`:

```python
class BlobBackend(Protocol):
    async def put_bytes(... ) -> BlobObject: ...
    async def put_file(... ) -> BlobObject: ...
    async def get_bytes(... ) -> bytes: ...
    async def download_file(... ) -> Path: ...
    async def stat(... ) -> BlobObject | None: ...
    async def exists(... ) -> bool: ...
    async def delete(... ) -> None: ...
    async def list_page(... ) -> Page[BlobObject]: ...
```

Implementation rule:

- `put_file` and `download_file` may use simple Python file I/O in v1
- backends may optimize internally, but the public contract should stay file-path and bytes based
- list cursors remain opaque Skaal cursors, not raw provider tokens

## Blob semantics

The v1 contract should stay intentionally narrow:

- object keys are opaque strings
- listing is lexicographic by key within a prefix
- `put_*` overwrites by key
- `delete` is idempotent
- metadata is a flat `dict[str, str]`
- content type is best-effort and user-supplied by default

Do not add conditional writes, append semantics, object versioning, or ACL policy APIs in this pass.

## Runtime wiring

Extend local and mesh runtime wiring to recognize `BlobStore` subclasses.

### Local runtime

- add a local filesystem backend, for example rooted at `./.skaal/blobs/<qualified_name>/`
- wire `BlobStore` classes during `LocalRuntime._patch_storage()`
- allow `backend_overrides` to replace the default, matching the current storage behavior

### Mesh/runtime wiring

- follow the same `__skaal_storage__` detection path already used for `Store`, relational, and vector resources
- keep blob runtime binding on the class, not on instances

## Backend implementations

### 1. Local filesystem backend

Add a simple backend for development, tests, and examples.

Rules:

- each blob store namespace gets its own directory
- object key paths are normalized and must not escape the namespace root
- metadata can live in sidecar JSON files or be reconstructed from filesystem stats in v1
- `list_page` walks files in sorted key order

This backend is required so blob storage is usable locally on day one.

### 2. S3 backend

Add a backend using `boto3`.

Rules:

- bucket name and prefix come from deploy/wire parameters
- use `put_object`, `get_object`, `head_object`, `delete_object`, and `list_objects_v2`
- map object metadata and ETag into `BlobObject`
- use Skaal cursor encoding around the last seen key rather than exposing raw S3 continuation tokens publicly

### 3. GCS backend

Add a backend using `google-cloud-storage`.

Rules:

- bucket name and prefix come from deploy/wire parameters
- use native blob metadata for size, content type, etag, and updated timestamp
- listing order and cursor semantics must match the S3 and local contracts

## Planner, catalog, and deploy implications

This pass should plug blob storage into the same planner path as other storage kinds.

### Catalog

Add blob-capable backend entries to the built-in catalogs:

- a local filesystem blob backend for local development
- an S3 backend for AWS catalogs
- a GCS backend for GCP catalogs

Each backend spec should advertise:

- `storage_kinds = ["blob"]`
- read/write latency ranges
- durability
- residency support
- deploy and wire params needed to provision and mount the bucket

### Solver

No new solving model is needed. The current `kind` constraint already exists. This pass only needs blob backends to advertise `storage_kinds = ["blob"]` and the blob decorator to emit `kind="blob"`.

### Deploy

Deploy builders must provision the object container and pass its identity into runtime wiring.

AWS:

- provision an S3 bucket
- export bucket name and optional prefix
- grant the runtime principal object read/write permissions

GCP:

- provision a GCS bucket
- export bucket name and optional prefix
- grant the runtime principal object read/write permissions

Local:

- create the filesystem root path
- mount it into the local runtime container/process if needed

Do not silently fall back from a blob resource to a KV backend. Missing blob support should fail during plan/build.

## Example and docs work

Add one new example focused on file storage, not generic CRUD.

Recommended example:

- `examples/06_file_upload_api/`
- mounted FastAPI app
- one `BlobStore` for uploads
- upload endpoint writes to `BlobStore.put_file(...)`
- download endpoint reads via `download_file(...)` or `get_bytes(...)`
- list endpoint shows paged object metadata

Docs to update:

- README storage overview
- user-facing storage docs
- mounted-framework guidance showing how blob storage pairs with FastAPI file uploads

## Test plan

Add focused tests instead of only end-to-end deploy coverage.

Required coverage:

- `tests/blob/test_blob_store.py` for the user-facing class API
- `tests/backends/test_blob_backend_contract.py` for local, S3-fake, and GCS-fake behavior
- local runtime test covering blob wiring and backend overrides
- solver/deploy tests verifying `kind="blob"` selects only blob-capable backends

Use fake clients for S3 and GCS where possible; do not make cloud infrastructure a prerequisite for the core test suite.

## Rollout order

Implement in this sequence:

1. add `BlobObject`, `BlobStore`, and `@app.blob`
2. add the `BlobBackend` protocol and local filesystem backend
3. wire local and mesh runtimes
4. add catalog entries and planner support for `kind="blob"`
5. add S3 and GCS backends
6. add deploy builders and example/docs/tests

This keeps the first validation target local and cheap before cloud/deploy work lands.

## Non-goals and follow-ups

Leave these for later ADRs once the tier exists:

- presigned upload/download URLs
- browser-direct multipart upload flows
- lifecycle rules and automatic retention expiry
- public bucket/CDN policy helpers
- checksum enforcement and conditional writes
- cross-region replication

The right first milestone is simply: a user can declare a blob resource in Skaal, run it locally, and deploy it to AWS or GCP without hand-wiring a separate object-storage stack.
