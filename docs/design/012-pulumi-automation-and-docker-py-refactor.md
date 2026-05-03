# ADR 012 — Single-Process Deploy: Pulumi Automation API + docker-py

**Status:** Proposed
**Date:** 2026-04-29
**Related:** [ADR 008](008-local-pulumi-migration.md), [ADR 010](010-deploy-submodule-refactor.md), [ADR 011](011-deploy-submodule-implementation-plan.md)

## Context

`skaal.deploy` runs deploys as a fan-out of subprocesses today. Every
non-local target shells out to the `pulumi` CLI; every target except AWS
shells out to the `docker` CLI; GCP additionally shells out to `gcloud`; AWS
shells out to `python -m pip`. The two halves of the package — the Automation
API used by `local`, and the CLI used by AWS/GCP — share no orchestration
code. Concretely:

| Subprocess site                                          | Purpose                                      |
| -------------------------------------------------------- | -------------------------------------------- |
| `skaal/deploy/pulumi/cli.py:run` / `run_live`            | `pulumi login/stack/config/up/destroy`       |
| `skaal/deploy/pulumi/cli.py:CliPulumiRunner`             | Orchestrates AWS + GCP deploys end-to-end    |
| `skaal/deploy/packaging/local.py`                        | `docker build`, `docker image inspect`       |
| `skaal/deploy/packaging/gcp.py`                          | `gcloud auth configure-docker`, `docker build`, `docker push` |
| `skaal/deploy/packaging/aws.py`                          | `python -m pip install -t …` (×2)            |
| `skaal/deploy/pulumi/automation.py:_docker_network_id` / `_docker_volume_name` | `docker network/volume inspect` for import detection |

The recent encoding bug ("'charmap' codec can't decode byte 0x81") and the
`.pulumi-state` mkdir-vs-login mismatch are symptoms of this split: every
subprocess wrapper is its own platform integration that has to get encoding,
working directory, environment, and pre-conditions right independently.

Secondary problems with the current layout:

- **Two Pulumi paths.** `CliPulumiRunner` (CLI) and `LocalAutomationRunner`
  (Automation API) implement the same lifecycle (login → stack select/init →
  config → up → outputs → destroy) twice. Adding a feature like preview, or
  per-stack `PULUMI_CONFIG_PASSPHRASE` handling, requires two edits.
- **Stringly-typed plumbing.** `run_live` returns `CompletedProcess[str]` and
  callers re-parse stdout. Errors surface as `CalledProcessError` carrying a
  blob of mixed stdout/stderr instead of a structured Pulumi diagnostic.
- **No streaming hook for the API path either.** `LocalAutomationRunner`
  passes `on_output=typer.echo`, hard-wiring CLI presentation into the
  deploy core — the Python API path has no way to capture the same stream.
- **Tool prerequisites are implicit.** `pulumi`, `docker`, `gcloud`, and a
  network-reachable `pip` index must exist on PATH; there is no single
  diagnostic helper, and failure modes vary by tool (`FileNotFoundError`,
  non-zero exit, encoding crash, …).
- **Tests fake subprocess.** `tests/deploy/test_push.py` etc. monkey-patch
  `cli.run` to return fake `CompletedProcess` objects; the tests pass without
  ever exercising real argument shapes.

## Decision

Replace every subprocess invocation in the deploy path with an in-process
Python call:

1. **Pulumi → Automation API everywhere.** Delete `CliPulumiRunner`. Promote
   the Automation API path to the single `PulumiRunner` implementation,
   parametrised by target. AWS and GCP move to the same runner currently used
   by local.
2. **Docker → docker-py (`docker` Python SDK).** Replace `docker build/push/
   inspect` shell-outs with `docker.from_env()` calls. The Pulumi
   `pulumi_docker.Image` resource (which already runs in-process via
   Automation) becomes the canonical builder for cloud targets; the
   standalone local builder also uses the SDK.
3. **gcloud auth → google-auth + Artifact Registry SDK.** Replace `gcloud
   auth configure-docker` with a docker-py `login()` call using a token
   obtained from `google.auth.default()`.
4. **pip install -t → `uv` Python API or `pip._internal` is out of scope;
   use the existing in-process subprocess only as a fallback.** Lambda
   packaging is the one site where shelling to a separate Python install is
   defensible because we deliberately want a clean, target-platform wheel
   resolution (`--platform manylinux2014_*`, `--python-version`). Wrap it
   behind a single `packaging/pip_runner.py` helper with explicit UTF-8
   handling and structured error reporting; do not replace it.

User-defined pre/post-deploy **hooks** (`api._run_hooks`) keep using
`subprocess.run` — those are user-supplied commands and must execute as
external processes.

## Why now

- ADR 010/011 have already partitioned deploy into `pulumi/`, `packaging/`,
  `targets/`, `builders/`. The runner contract (`PulumiRunner` Protocol) is a
  single interface, so collapsing two implementations into one is a focused
  change rather than a cross-cutting one.
- The encoding crash on Windows is a recurring class of bug for any code
  path that pipes a child process's stdout through Python with the default
  locale. Removing the children removes the class.
- Authentication for AWS/GCP via the Python SDKs (`boto3`, `google.auth`) is
  already a transitive dependency through `pulumi-aws` / `pulumi-gcp`. We are
  not adding new credential surfaces.

## Non-Goals

- Replacing `pulumi` itself. We keep Pulumi as the IaC engine; only the
  process boundary changes.
- Removing the Pulumi state file backend (`file://…/.pulumi-state`). The
  Automation API supports it directly.
- Changing the artifact format produced by `skaal build`. Generated
  `Pulumi.yaml`, `pyproject.toml`, `Dockerfile`, etc. stay as they are.
- Replacing user-defined hook execution. `_run_hooks` keeps `subprocess.run`.
- Replacing the Lambda packaging `pip install -t` shell-out.
- Migrating tests beyond what is needed to exercise the new in-process paths.
  Existing structural tests for builders/wiring continue to work unchanged.

## Target Architecture

### Single `PulumiRunner`

```python
# skaal/deploy/pulumi/runner.py  (new)

class AutomationRunner(PulumiRunner):
    def deploy(self, plan: RunnerPlan) -> StackOutputs:
        ctx = plan.context
        spec = _read_stack_spec(ctx.artifacts_dir)            # already exists
        program = _program_for(spec, plan.context)            # generic
        stack = auto.create_or_select_stack(
            stack_name=ctx.stack,
            project_name=spec["name"],
            program=program,
            opts=_workspace_options(ctx.artifacts_dir, spec),
        )
        for key, value in plan.config.items():
            stack.set_config(key, auto.ConfigValue(value=value))
        if plan.package is not None:
            plan.package(ctx)
        stack.up(on_output=_emit, on_event=_emit_event)       # logger, not typer
        if plan.post_up is not None and plan.post_up(ctx, lambda k: stack.outputs()[k].value):
            stack.up(on_output=_emit, on_event=_emit_event)
        return {key: str(stack.outputs()[key].value) for key in plan.output_keys}

    def destroy(self, artifacts_dir: Path, *, stack: str, yes: bool) -> None:
        spec = _read_stack_spec(artifacts_dir)
        stack_ref = auto.select_stack(
            stack_name=stack,
            project_name=spec["name"],
            program=_program_for(spec, None),
            opts=_workspace_options(artifacts_dir, spec),
        )
        stack_ref.destroy(on_output=_emit, on_event=_emit_event)
```

`_program_for(spec, ctx)` returns a Pulumi inline program that handles all
three target shapes. For AWS and GCP today the program is generated as
`Pulumi.yaml` plus a Python entrypoint; under this ADR the inline program
imports the same builders (`skaal.deploy.builders.aws|gcp|local`) and
constructs Pulumi resources directly — no YAML eval step.

The `Pulumi.yaml` file remains as a build artifact for users who want to run
`pulumi up` by hand, but the runner does not consume it.

### docker-py builder

```python
# skaal/deploy/packaging/docker_builder.py  (new)

import docker

def build_image(
    *,
    context_dir: Path,
    tag: str,
    progress: ProgressSink,           # ABC defined in ADR 013
) -> str:
    client = docker.from_env()
    image, log_stream = client.images.build(
        path=str(context_dir),
        tag=tag,
        rm=True,
        forcerm=True,
        decode=True,                  # yields dicts, not bytes
    )
    for chunk in log_stream:
        progress.docker_log(chunk)    # routed to logging in ADR 013
    return image.id
```

For GCP, the same client logs in to Artifact Registry with a Google token,
then `client.images.push(repository=..., tag=...)`. No `gcloud` invocation.

### `_docker_network_id` / `_docker_volume_name`

Replace the two `subprocess.run(["docker", ...])` helpers in
`pulumi/automation.py` with `client.networks.list(names=[name])` /
`client.volumes.list(filters={"name": name})`. Both return Python objects;
`None` becomes a clean empty-list check.

### Module layout (delta on top of ADR 010)

```
skaal/deploy/
├── pulumi/
│   ├── runner.py         # AutomationRunner (replaces CliPulumiRunner)
│   ├── automation.py     # workspace + state-dir helpers, kept
│   ├── env.py            # backend URL helpers, kept
│   └── meta.py           # unchanged
│   # cli.py is deleted
├── packaging/
│   ├── docker_builder.py # docker-py wrapper (new)
│   ├── gcp_push.py       # GAR auth + push via docker-py (renamed from gcp.py)
│   ├── lambda_pkg.py     # pip install -t (renamed from aws.py, scope unchanged)
│   └── local.py          # uses docker_builder, no shell-out
└── targets/registry.py   # both AWS and GCP point at AutomationRunner
```

## Per-Target Behaviour Changes

### local

- Already uses Automation API; `_docker_network_id` and `_docker_volume_name`
  switch to docker-py. No user-visible change.
- `build_local_image` calls `docker_builder.build_image` instead of
  `run_live(["docker", "build", …])`.

### gcp

- `_gcp_post_up` no longer calls `build_and_push_image`. Instead it gets the
  Artifact Registry repository URL from the first `pulumi up`, calls
  `docker_builder.build_image` for the local context, authenticates the
  docker-py client with a Google token, and calls
  `client.images.push(repository, tag)`.
- The CLI runner is gone. The two `pulumi up` invocations become two
  `stack.up(...)` calls on the same `Stack` object held in memory across
  the post-up step.
- `gcloud` is no longer required on PATH.

### aws

- The CLI runner is gone. `_aws_package` (Lambda packaging via `pip
  install -t`) keeps its single subprocess call, now wrapped behind
  `packaging/lambda_pkg.run_pip` which forces `encoding="utf-8"` and
  `errors="replace"` like the recent fix in `pulumi/cli.py`.
- `pulumi` CLI is no longer required on PATH.

## Dependency Changes

Add to `pyproject.toml`:

- `docker>=7.1` — official Docker SDK for Python.
- `google-auth>=2.32` — already pulled transitively via `google-cloud-*` but
  pin explicitly so the auth helper has a known floor.

`pulumi-aws`, `pulumi-gcp`, `pulumi-docker` are already direct or transitive
deps of generated artifacts and the Automation API. The Automation API ships
with the `pulumi` Python package and does not require the `pulumi` CLI — it
downloads its own Pulumi engine binary on first use.

Remove from runtime requirements / docs:

- "Install the `pulumi` CLI"
- "Install `gcloud` CLI"
- "Have `docker` CLI on PATH" (the Docker daemon is still required; the
  client binary is not)

## Error Handling

The Automation API surfaces Pulumi failures as
`pulumi.automation.errors.CommandError` subclasses with structured
`stdout`/`stderr` strings on the exception. Wrap each `stack.up` /
`stack.destroy` call once, and translate to a `DeployError` (new — defined
in `skaal.deploy.errors`) carrying:

```python
@dataclass
class DeployError(Exception):
    target: TargetName
    phase: Literal["preview", "up", "destroy", "package", "image"]
    message: str
    diagnostics: str | None    # e.g. structured Pulumi engine event
```

`docker.errors.BuildError` / `docker.errors.APIError` map to the same
exception with `phase="image"`. The CLI catches `DeployError` and presents
it via the logger introduced in ADR 013; the Python API re-raises it
unchanged.

## Tests

- Replace `tests/deploy/test_push.py` monkey-patches that fake
  `subprocess.run`/`CompletedProcess` with fakes that satisfy the
  `PulumiRunner` Protocol and a fake `docker_builder` callable. The tests
  assert on calls into these fakes, not on argv shapes.
- Add a thin integration test (skipped unless `SKAAL_DOCKER_E2E=1`) that
  builds a one-line Dockerfile through `docker_builder.build_image` against
  the real local daemon. This is the only test that exercises docker-py end
  to end and is opt-in.
- Existing builder/wiring tests are unaffected — they assert on the dicts
  passed to Pulumi, not on how those dicts get materialised.

## Migration Steps

This is a direct cutover (per ADR 011, no shims).

1. Add `docker>=7.1` to `pyproject.toml`.
2. Land `skaal/deploy/packaging/docker_builder.py` and rewrite
   `packaging/local.py` to use it. Update `tests/deploy/test_local_stack.py`
   fakes.
3. Replace `_docker_network_id` / `_docker_volume_name` in
   `pulumi/automation.py` with docker-py calls.
4. Land `skaal/deploy/packaging/gcp_push.py` (replaces current `gcp.py`).
   Update `tests/deploy/test_gcp_apigw.py` fakes.
5. Land `skaal/deploy/pulumi/runner.py` with `AutomationRunner`. Wire AWS
   and GCP target strategies to it in `targets/registry.py`. Delete
   `pulumi/cli.py`. Update `tests/deploy/test_aws_*` and `test_gcp_*` fakes.
6. Replace the `pulumi up`/`pulumi destroy` CLI invocations remaining inside
   the Lambda packaging step (none today; verified) and confirm
   `tests/deploy/test_push.py` only references the runner Protocol.
7. Delete the `pulumi`/`gcloud`/`docker` PATH preconditions from `README.md`
   and `skaal/cli/deploy_cmd.py` docstrings.

Each step is a separate PR; the order keeps `main` runnable at every step
because each new in-process implementation is wired in before the
corresponding subprocess wrapper is removed.

## Open Questions

- **Pulumi engine bootstrap on Windows.** The Automation API downloads its
  own Pulumi binary into `~/.pulumi`. Confirm the download path is honoured
  in CI environments where outbound HTTPS is restricted; if not, document a
  `PULUMI_HOME` override.
- **Docker daemon on macOS without Docker Desktop.** docker-py talks to the
  daemon over a Unix socket or `DOCKER_HOST` URL. Users running `colima`,
  `rancher-desktop`, or `podman` already set `DOCKER_HOST` — no special
  handling required, but worth a one-line note in the deploy docs.
- **Streaming output contract.** `stack.up(on_output=…)` and
  `client.images.build(...)` produce different stream shapes. ADR 013
  defines the `ProgressSink` ABC referenced above; this ADR depends on that
  decision but does not block on it (a no-op sink is acceptable for the
  first cut).
