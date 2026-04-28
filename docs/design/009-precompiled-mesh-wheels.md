# ADR 009 — Pre-compile `skaal-mesh` Wheels in CI; Drop Rust from Runtime Images

**Status:** Proposed
**Date:** 2026-04-28

## Context

`skaal-mesh` is a PyO3 Rust extension that lives in [mesh/](../../mesh/) and is
declared in the root [pyproject.toml](../../pyproject.toml#L66-L69) as the
`mesh` optional dependency. It is wired as an editable local source via
[tool.uv.sources](../../pyproject.toml#L115-L116):

```toml
[tool.uv.sources]
skaal-mesh = { path = "mesh", editable = true }
```

This means **every install path compiles Rust from source**:

1. **Developers** — `uv sync --group dev` runs `maturin develop`, which needs
   `cargo`, `rustc`, and a C toolchain.
2. **Generated deployment artifacts** — both
   [skaal/deploy/local.py:430-454](../../skaal/deploy/local.py#L430-L454)
   and [skaal/deploy/gcp.py:527-573](../../skaal/deploy/gcp.py#L527-L573)
   `shutil.copytree(mesh_src_dir, output_dir / "mesh")` and rewrite
   `[tool.uv.sources]` in the generated `pyproject.toml` to point at `./mesh`.
3. **Generated Docker images** — both
   [skaal/deploy/templates/local/Dockerfile](../../skaal/deploy/templates/local/Dockerfile)
   and [skaal/deploy/templates/gcp/Dockerfile](../../skaal/deploy/templates/gcp/Dockerfile)
   contain the same conditional `if [ -d "mesh" ]; then apt-get install
   build-essential && curl rustup` block, and mount `~/.cargo/registry` +
   `~/.cargo/git` build caches so `uv sync` can compile the crate at image
   build time.
4. **AWS Lambda** — does not work today. The note at
   [skaal/deploy/aws.py:675-701](../../skaal/deploy/aws.py#L675-L701)
   acknowledges Lambda zip packages cannot compile Rust at deploy time and
   recommends either container images or a hand-rolled Lambda Layer.

The cost of compile-on-install:

- ~5–8 min added to every cold Docker build (rustup download + crate
  compile), even though the source rarely changes.
- Image size bloat: `build-essential` + `~/.cargo` add hundreds of MB unless
  carefully discarded; the current Dockerfiles do not multi-stage them away.
- Developer onboarding requires a Rust toolchain just to run `uv sync`.
- AWS Lambda is effectively blocked.
- CI is not exercising the same artifact users will run.

## Decision

Build platform-specific `skaal-mesh` wheels in CI on every release tag, attach
them as GitHub Release assets, and publish to PyPI. The runtime path
(developer install, generated Docker images, AWS Lambda) consumes the
prebuilt wheel from PyPI like any other binary dependency. The `mesh/`
directory becomes a build-time concern only, never copied into deployment
artifacts.

**No backward compatibility.** The `mesh/`-bundling, in-image Rust toolchain,
editable `[tool.uv.sources]` for `skaal-mesh`, and the AWS Lambda Layer
workaround are all removed outright.

### Wheel matrix

`skaal-mesh` ships wheels for the platforms Skaal actually targets at
runtime. Unsupported platforms get a clean `pip` error rather than a
fallback sdist build.

| OS              | Arch    | Python    | Use case                              |
|-----------------|---------|-----------|---------------------------------------|
| `manylinux2014` | x86_64  | 3.11–3.13 | local Docker, GCP Cloud Run, Lambda   |
| `manylinux2014` | aarch64 | 3.11–3.13 | Graviton Lambda, ARM Cloud Run        |
| `musllinux_1_2` | x86_64  | 3.11–3.13 | Alpine images, distroless variants    |
| `macosx_11_0`   | arm64   | 3.11–3.13 | dev on Apple Silicon                  |
| `macosx_11_0`   | x86_64  | 3.11–3.13 | dev on Intel Macs                     |
| `win_amd64`     | x86_64  | 3.11–3.13 | dev on Windows                        |

`abi3` (PyO3 stable ABI) is **not** used — pinning per-Python-minor wheels
keeps PyO3 fast paths and lets us drop 3.11 without an abi3 floor migration.

No sdist for `skaal-mesh`. Skipping sdist is what guarantees `pip install`
on an unsupported platform fails immediately instead of silently trying to
compile.

### Distribution

`skaal-mesh` is published to PyPI as its own distribution alongside `skaal`,
under the same release tag. A single `v0.X.Y` tag triggers both:

- `skaal` wheel + sdist (pure Python, unchanged path).
- `skaal-mesh` wheels for the matrix above.

Both are uploaded to PyPI via OIDC trusted publishing, then attached to the
GitHub Release.

### Source-of-truth versioning

`mesh/pyproject.toml` and `Cargo.toml` both carry `version = "0.1.0"` today.
After this change, the `skaal-mesh` version is bumped in lockstep with
`skaal` itself — release tooling enforces it. A mismatched pair (`skaal`
0.3.0 + `skaal-mesh` 0.2.0) is rejected at release time. The runtime does
not need a compatibility matrix; one tag, one pair.

## Implementation plan

### 1. New CI workflow: `mesh-wheels.yml`

Add `.github/workflows/mesh-wheels.yml` driven by `PyO3/maturin-action`,
which is the canonical wheel-builder for PyO3 projects and handles
manylinux/musllinux containerisation, cross-compilation for aarch64, and
the macOS universal2 split.

Triggers:

- `push` of a tag matching `v*` (release path).
- `workflow_dispatch` (manual rebuild).
- `pull_request` touching `mesh/**`, `Cargo.toml`, or
  `.github/workflows/mesh-wheels.yml` (PR validation — builds wheels but
  does not publish).

Job shape (one matrix job per `(os, target)` pair):

```yaml
strategy:
  matrix:
    include:
      - { os: ubuntu-latest,  target: x86_64-unknown-linux-gnu,   manylinux: 2014 }
      - { os: ubuntu-latest,  target: aarch64-unknown-linux-gnu,  manylinux: 2014 }
      - { os: ubuntu-latest,  target: x86_64-unknown-linux-musl,  manylinux: musllinux_1_2 }
      - { os: macos-14,       target: aarch64-apple-darwin }
      - { os: macos-13,       target: x86_64-apple-darwin }
      - { os: windows-latest, target: x86_64-pc-windows-msvc }
steps:
  - uses: actions/checkout@v4
  - uses: PyO3/maturin-action@v1
    with:
      working-directory: mesh
      target: ${{ matrix.target }}
      manylinux: ${{ matrix.manylinux || 'auto' }}
      args: --release --out dist --interpreter 3.11 3.12 3.13
  - uses: actions/upload-artifact@v4
    with:
      name: mesh-${{ matrix.target }}
      path: mesh/dist/*.whl
```

A final `publish-mesh` job downloads all `mesh-*` artifacts, flattens them
into `dist/`, runs `twine check`, and publishes with
`pypa/gh-action-pypi-publish@release/v1` using the same `pypi` environment
already configured in [release.yml](../../.github/workflows/release.yml#L52).

### 2. Wire `skaal-mesh` publish into the existing release flow

Two viable shapes — pick **(a)**:

**(a) Single workflow.** Move the matrix job into `release.yml` so a `v*`
tag produces `skaal` wheel + `skaal-mesh` wheels in one run, and both
`publish-pypi` jobs run from the same artifact set. PyPI ordering is
irrelevant (independent distributions).

**(b) Two workflows.** Keep `mesh-wheels.yml` separate; trigger from the
same tag. Slightly cleaner separation of concerns but two PyPI environments
to keep in sync.

(a) is preferred because `skaal` and `skaal-mesh` versions are released
together; one workflow makes the lockstep explicit.

### 3. Root `pyproject.toml` changes

Remove the editable local source so a fresh `uv sync` pulls
`skaal-mesh` from PyPI like any other dependency:

```diff
-[tool.uv.sources]
-skaal-mesh = { path = "mesh", editable = true }
```

Tighten the version pin in the `mesh` extra and the `dev` group to the
released wheel version (kept in lockstep with `skaal`):

```diff
 [project.optional-dependencies]
 mesh = [
-    # Rust mesh extension — compiled from source via maturin on `uv sync`
-    "skaal-mesh",
+    "skaal-mesh==0.2.0",
 ]
```

Same change in the `dev` group. Developers who need to iterate on the Rust
code use `make build-dev` (already wired to `maturin develop`); that still
works because `mesh/` is a real maturin project, it just isn't an automatic
side effect of `uv sync` anymore.

### 4. Generated artifact: stop bundling `mesh/`

[skaal/deploy/local.py:430-454](../../skaal/deploy/local.py#L430-L454) and
[skaal/deploy/gcp.py:527-573](../../skaal/deploy/gcp.py#L527-L573) both
copy the `mesh/` source tree into the artifact directory and inject
`uv_sources["skaal-mesh"] = "./mesh"`. Delete those blocks.

The mesh dependency line stays — `infra_deps.append("skaal-mesh")` — but
it is now a normal PyPI dep resolved by `uv sync` from the index.

How "is mesh wanted?" is decided after this change: the deploy code stops
using `mesh_src_dir.is_dir()` as the signal (artifacts no longer carry the
source). Replace it with a single explicit signal — the `mesh` extra being
present in the runtime config (e.g. `skaal[mesh]` in the consuming user
project, or an `enable_mesh: bool` on the deploy config). Default off.

The `dev` bundle path in `local.py:418-428` that rewrites `path = "mesh"`
to `path = "../mesh"` in the bundled `pyproject.toml` is also deleted —
no `[tool.uv.sources]` entry for `skaal-mesh` exists to rewrite.

### 5. Generated Dockerfile templates

[templates/local/Dockerfile:20-35](../../skaal/deploy/templates/local/Dockerfile#L20-L35)
and [templates/gcp/Dockerfile:15-30](../../skaal/deploy/templates/gcp/Dockerfile#L15-L30):

- Delete the `if [ -d "mesh" ]; then apt-get install build-essential
  && curl rustup` block.
- Delete `ENV PATH="/root/.cargo/bin:$${PATH}"`.
- Delete the `--mount=type=cache,target=/root/.cargo/registry` and
  `target=/root/.cargo/git` cache mounts on the `uv sync` step.
- Delete the apt cache mounts (only the rust install used apt; uv sync no
  longer needs apt).

Both images land at `python:3.11-slim` + `pip install uv` + `uv sync
--no-dev`, with no system-package step. The wheel for the right
manylinux/glibc is fetched from PyPI by uv in the same step that pulls
`pydantic` etc.

The pinned base image must be glibc-based for the `manylinux2014` wheel to
satisfy. `python:3.11-slim` is Debian (glibc) — fine. If we ever switch to
`python:3.11-alpine`, that requires `musllinux_1_2` wheels, which the
matrix already builds.

### 6. AWS Lambda — now actually works

The note at [skaal/deploy/aws.py:675-701](../../skaal/deploy/aws.py#L675-L701)
becomes obsolete. Lambda zip packaging works as long as the `pip install`
that builds the deployment package targets `manylinux2014_x86_64` (or
`manylinux2014_aarch64` for Graviton functions).

Rewrite the section to:

- Remove the entire "NOTE — skaal-mesh on Lambda" block.
- When `skaal-mesh` is in `base_deps`, ensure the Lambda packaging step
  passes `--platform manylinux2014_x86_64 --only-binary=:all:` to `pip` (or
  the uv equivalent) so the right wheel is fetched on a non-Linux build
  host. The Lambda architecture (x86_64 vs arm64) is already a Pulumi
  config; reuse it to choose the platform tag.

The "mesh state is ephemeral on Lambda" caveat is unrelated to wheels and
stays — but it belongs in user-facing docs, not a comment in the codegen
file.

### 7. Developer workflow

- `uv sync` (no group) — pulls `skaal-mesh` from PyPI, no Rust needed.
- `uv sync --group dev` — same, plus dev tools. **Rust is no longer a
  prerequisite for running tests** unless the developer is editing the
  Rust crate.
- `make build-dev` (`maturin develop`) — what mesh contributors run after
  editing `mesh/src/`. Installs an editable wheel into the active venv,
  shadowing the PyPI version. Document this in `CONTRIBUTING.md`.
- `make build` (`maturin build --release`) — local sanity check before
  pushing a tag.

### 7a. `skaal build --dev` interaction

The `--dev` flag on `skaal build` exists for **iterating on `skaal`
itself**, not user code
([build_cmd.py:49-57](../../skaal/cli/build_cmd.py#L49-L57)). Today it
does three things in [local.py](../../skaal/deploy/local.py):

1. Copies the working tree of `skaal/` into `artifacts/_skaal/skaal/` and
   the root `pyproject.toml` into `artifacts/_skaal/pyproject.toml`
   ([local.py:415-428](../../skaal/deploy/local.py#L415-L428)).
2. Rewrites `path = "mesh"` → `path = "../mesh"` in that bundled
   pyproject so its `[tool.uv.sources]` still resolves the mesh crate one
   directory up ([local.py:421-423](../../skaal/deploy/local.py#L421-L423)).
3. Adds `uv_sources["skaal"] = "./_skaal"` so the artifact's `uv sync`
   installs skaal from the bundled copy
   ([local.py:451-452](../../skaal/deploy/local.py#L451-L452)) and bind-mounts
   the host `skaal/` tree into the container at `/app/skaal` for hot reload
   ([local.py:237-238](../../skaal/deploy/local.py#L237-L238)).

After the migration:

- (1) and (3) **stay unchanged** — bundling pure-Python skaal source is
  orthogonal to the mesh wheel question.
- (2) is **deleted**. The root `pyproject.toml` no longer has
  `[tool.uv.sources]`, so there is no `path = "mesh"` line to rewrite.
  The bundled pyproject is copied verbatim.

**The non-obvious consequence — Rust edits no longer flow through `--dev`.**

Today, a contributor editing `mesh/src/` and running `skaal build --dev`
gets a fresh image where `uv sync` recompiles the local mesh crate.
After the migration, the artifact resolves `skaal-mesh==<pinned>` from
PyPI and **silently ignores the contributor's local Rust changes**. The
host bind mount only covers `/app/skaal`, not the compiled extension
inside the venv.

Mesh contributors get one of two loops:

**(a) Python-side iteration only** — `make build-dev` once, then run
tests / examples directly on the host. The editable wheel from `maturin
develop` shadows the PyPI version in the active venv.

**(b) Docker-side iteration** — temporarily pin `skaal-mesh` to a
TestPyPI pre-release tag (cut by `mesh-wheels.yml` on `workflow_dispatch`),
or add a one-line local override to the bundled artifact pyproject:

```toml
[tool.uv.sources]
skaal-mesh = { path = "../mesh" }
```

This is a manual escape hatch, not a `--dev` feature. Adding a separate
flag (`--dev-mesh`?) that re-enables source bundling is rejected: the
whole point of this ADR is that the in-image Rust toolchain disappears,
and re-adding it behind a flag preserves the slow path nobody wants.

Document loop (a) as the supported path in `CONTRIBUTING.md`. Loop (b)
is a "you know what you're doing" workaround.

### 8. Tests

- Add `tests/deploy/test_no_rust_in_dockerfile.py` — render the local and
  gcp Dockerfile templates and assert the strings `rustup`,
  `build-essential`, and `cargo` do not appear.
- Add `tests/deploy/test_no_mesh_bundle.py` — invoke the local and gcp
  build paths against a fixture project and assert no `mesh/` directory is
  present in the output, and the generated `pyproject.toml` has no
  `[tool.uv.sources]` block (or the block does not contain `skaal-mesh`).
- Update any existing test that asserts the mesh source is bundled — flip
  the assertion.
- Add a smoke job to `ci.yml` that runs `pip install --platform
  manylinux2014_x86_64 --only-binary=:all: skaal-mesh==<version>` against
  TestPyPI after a release-candidate tag, to catch missing wheels in the
  matrix before a real release.

## What gets removed

Concrete deletions, no replacement:

| Location | What |
|---|---|
| [pyproject.toml:115-116](../../pyproject.toml#L115-L116) | Entire `[tool.uv.sources]` block (only entry is `skaal-mesh`). |
| [pyproject.toml:67-68](../../pyproject.toml#L67-L68) | "compiled from source via maturin on `uv sync`" comment. |
| [pyproject.toml:83-84](../../pyproject.toml#L83-L84) | Same comment in the `dev` group. |
| [skaal/deploy/local.py:418-428](../../skaal/deploy/local.py#L418-L428) | `_skaal` bundle path-rewrite of `path = "mesh"` → `path = "../mesh"`. |
| [skaal/deploy/local.py:430-435](../../skaal/deploy/local.py#L430-L435) | `mesh_bundle_dir` `shutil.copytree`. |
| [skaal/deploy/local.py:453-454](../../skaal/deploy/local.py#L453-L454) | `uv_sources["skaal-mesh"] = "./mesh"`. |
| [skaal/deploy/gcp.py:527-535](../../skaal/deploy/gcp.py#L527-L535) | mesh bundle copytree block. |
| [skaal/deploy/gcp.py:573](../../skaal/deploy/gcp.py#L573) | `uv_sources` literal containing `skaal-mesh`. |
| [skaal/deploy/aws.py:675-684](../../skaal/deploy/aws.py#L675-L684) | "NOTE — skaal-mesh on Lambda" comment block. |
| [skaal/deploy/aws.py:685-686](../../skaal/deploy/aws.py#L685-L686) | `mesh_src_dir` / `has_mesh` filesystem probe. |
| [skaal/deploy/aws.py:698-701](../../skaal/deploy/aws.py#L698-L701) | "requires a Lambda Layer with the compiled skaal_mesh extension" comment. |
| [templates/local/Dockerfile:17-29](../../skaal/deploy/templates/local/Dockerfile#L17-L29) | `if [ -d "mesh" ]` apt + rustup install + cargo PATH. |
| [templates/local/Dockerfile:34-35](../../skaal/deploy/templates/local/Dockerfile#L34-L35) | `--mount=type=cache,target=/root/.cargo/registry` and `git`. |
| [templates/gcp/Dockerfile:12-24](../../skaal/deploy/templates/gcp/Dockerfile#L12-L24) | Same rustup install + cargo PATH. |
| [templates/gcp/Dockerfile:29-30](../../skaal/deploy/templates/gcp/Dockerfile#L29-L30) | Same cargo cache mounts. |
| ADR 006 §Consequences — "Rust build dependency increases CI time / Developers need Rust toolchain installed" lines | Update to reflect prebuilt wheels. |

The `mesh/` workspace member, `Cargo.toml`, `Cargo.lock`, `mesh/src/`, and
`mesh/pyproject.toml` all stay — they are now build inputs to the wheel
job, not runtime inputs to the consumer.

## Migration order (no flag-flip; one PR per step is fine)

1. Land the new `mesh-wheels.yml` (or the merged `release.yml` matrix) and
   cut a pre-release tag (`v0.2.0rc1`) to verify the wheel set publishes
   to TestPyPI cleanly.
2. Pin `skaal-mesh==0.2.0rc1` in `pyproject.toml`, drop `[tool.uv.sources]`,
   and run `uv sync` locally on Linux + macOS + Windows to confirm the
   wheel resolves.
3. Strip the Dockerfile rustup blocks. Run `skaal build --target local`
   and `--target gcp` against an example project; `docker build` the
   artifact and confirm the image succeeds without a Rust toolchain.
4. Strip the `mesh/` bundling from `local.py` / `gcp.py`. Same
   build-and-run validation.
5. Rewrite the AWS Lambda packaging path to use `--platform
   manylinux2014_<arch> --only-binary=:all:` and delete the Lambda Layer
   note.
6. Cut the real `v0.2.0` tag.

## Consequences

**Positive:**
- Cold image build drops by ~5–8 min (no rustup, no crate compile, no
  apt-get).
- Image size shrinks by ~300–500 MB (no `build-essential`, no `~/.cargo`).
- New developer onboarding: `uv sync`, no Rust toolchain, tests run.
- AWS Lambda zip packaging works with `skaal-mesh` for the first time.
- CI publishes the same artifacts users install — drift between
  developer-compiled and user-compiled binaries is gone.

**Negative:**
- Release pipeline grows from one wheel to ~18 (3 Pythons × 6 platforms),
  with corresponding CI minutes (~15–25 min total wall clock with matrix
  parallelism).
- A new platform target (e.g. linux/riscv64) requires a CI matrix entry
  before users can install on it; no transparent sdist fallback.
- Mesh contributors must remember `make build-dev` after editing Rust;
  `uv sync` no longer rebuilds for them. Documented in `CONTRIBUTING.md`.
- Tag-time coupling: a `skaal` patch release that does not touch `mesh/`
  still rebuilds the full wheel matrix. Acceptable cost.
