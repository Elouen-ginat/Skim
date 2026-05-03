# ADR 008 — Migrate Local Runtime from Docker Compose to Pulumi

**Status:** Proposed
**Date:** 2026-04-28

## Context

The deploy targets currently split along two implementation paths:

- **Cloud (`aws`, `gcp`)** — generate a `Pulumi.yaml` from a Python dict and
  apply it via `PulumiClient.up()`
  ([_aws_stack_builder.py:461](../../artifacts/_skaal/skaal/deploy/builders/_aws_stack_builder.py#L461)).
- **Local (`local-compose`)** — generate a `docker-compose.yml` from a string
  template and apply it via `docker compose up --build`
  ([deploy/builders/local_compose.py](../../artifacts/_skaal/skaal/deploy/builders/local_compose.py),
  [deploy/local_services.py](../../artifacts/_skaal/skaal/deploy/local_services.py)).

The two paths duplicate concepts (service catalog, gateway adapter, dependency
wiring) in two different shapes, and the local path has its own builder, its
own deployer, its own template, and its own teardown verb (`docker compose
down`). Every backend addition pays the cost twice.

Unifying on Pulumi for local infra collapses the divergence: one builder
pattern, one client (`PulumiClient`), one apply/destroy lifecycle, and one
mental model across `local`, `aws`, and `gcp`. The official `pulumi-docker`
provider exposes everything compose did — `Network`, `Volume`, `Image`,
`Container` (with env, ports, healthchecks, labels, depends_on, network
aliases) — so no custom dynamic providers are needed for the current scope.

## Decision

Replace the local Docker Compose target with a local Pulumi target that uses
the `pulumi-docker` provider against the host Docker daemon, with a filestate
(local-disk) Pulumi backend so no Pulumi Cloud account is ever required.

No backward compatibility shim. The `local-compose` target name and
`docker-compose.yml` artifact are removed. The replacement target is named
`local` (canonical) with alias `local-docker`.

### Shape

The local stack builder mirrors `_aws_stack_builder.py`. It produces:

```python
{
  "name": f"skaal-{app.name}",
  "runtime": "yaml",
  "plugins": {"providers": [{"name": "docker", "version": "4.5.0"}]},
  "resources": {
    "skaal-net":  {"type": "docker:Network", ...},
    "skaal-data": {"type": "docker:Volume", ...},
    "app-image":  {"type": "docker:Image",
                   "properties": {"build": {"context": "."},
                                  "imageName": "skaal-app:local",
                                  "skipPush": True}},
    "postgres":   {"type": "docker:Container", ...},  # only if a backend needs it
    "redis":      {"type": "docker:Container", ...},
    "app":        {"type": "docker:Container",
                   "properties": {"image": "${app-image.imageName}",
                                  "envs": [...],
                                  "ports": [{"internal": 8000, "external": port}],
                                  "networksAdvanced": [{"name": "${skaal-net.name}"}],
                                  "volumes": [...],
                                  "labels": [...]},
                   "options": {"dependsOn": [...]}},
  },
  "outputs": {"appUrl": f"http://localhost:{port}"},
}
```

File-backed stores (SQLite, Chroma) require **no** Pulumi resource — they live
inside the app container's mounted data volume. The only "real" infra
resources locally are postgres, redis, traefik/kong, the app image, the app
container, the network, and the data volume.

### Pulumi local backend

`PulumiClient` gains a `login_local(state_dir)` method that runs
`pulumi login file://<state_dir>`. The local deployer calls it before
`select_or_init_stack`. State lives in the artifacts directory as
`.pulumi-state/` (gitignored). `_run` injects
`PULUMI_CONFIG_PASSPHRASE=""` into the env when not already set so filestate
stack init does not prompt; cloud targets are unaffected because they do not
use filestate.

## Implementation plan

### Step 1 — Local Pulumi stack builder

Add `artifacts/_skaal/skaal/deploy/builders/_local_stack_builder.py` mirroring
[_aws_stack_builder.py](../../artifacts/_skaal/skaal/deploy/builders/_aws_stack_builder.py)
with a `_LocalStackContext` and `_build_pulumi_stack(app, plan)`.

Add `artifacts/_skaal/skaal/deploy/builders/local_stack.py` re-exporting
`_build_pulumi_stack` (matches the `aws_stack.py` shape).

### Step 2 — Replace the service catalog

Replace
[deploy/local_services.py](../../artifacts/_skaal/skaal/deploy/local_services.py)
with `deploy/builders/_local_resources.py`. Same keys (`postgres`, `redis`,
`traefik`, `kong`) but values become `docker:Container` property dicts: `image`,
`envs` (list of `"K=V"` strings), `ports`, `healthcheck`, `volumes`, `command`.

### Step 3 — Update gateway adapters

[deploy/builders/_gateways.py](../../artifacts/_skaal/skaal/deploy/builders/_gateways.py)
keeps the traefik label logic (labels remain labels — Pulumi accepts them).
Drop the `compose_service` field; replace with a method returning a
`docker:Container` resource dict. Kong's `kong.yml` is still written as a
sibling artifact and mounted into the kong container via a `hostPath` volume.

### Step 4 — Replace target builder and deployer

Rename
[deploy/targets/local_compose.py](../../artifacts/_skaal/skaal/deploy/targets/local_compose.py)
→ `deploy/targets/local_docker.py`.

`LocalDockerBuilder._generate_artifacts` writes:

- `main.py` / `Dockerfile` — unchanged (existing templates).
- `pyproject.toml` — unchanged.
- `Pulumi.yaml` — via existing `write_pulumi_stack_artifact()`.
- **No** `docker-compose.yml`. Delete `_build_docker_compose` and its file.
- Source bundle copy and dev-mount logic stay; the host-path mount moves from
  compose `volumes:` to the `docker:Container` `volumes` property.

`LocalDockerDeployer.deploy`:

```python
client = PulumiClient(artifacts_dir)
client.login_local(artifacts_dir / ".pulumi-state")
client.select_or_init_stack(options.stack)   # default "local"
client.up(yes=options.yes, ...)
url = client.output("appUrl")
reporter.result(f"App URL: {url}")
```

### Step 5 — Pulumi local-backend plumbing

In
[deploy/pulumi/client.py](../../artifacts/_skaal/skaal/deploy/pulumi/client.py):

1. Add `PulumiClient.login_local(state_dir: Path)` running
   `pulumi login file://<state_dir>`.
2. `_run` injects `PULUMI_CONFIG_PASSPHRASE=""` into the env if unset.

Add `.pulumi-state/` to the generated `.gitignore`.

### Step 6 — Config tweaks

[deploy/config.py](../../artifacts/_skaal/skaal/deploy/config.py#L356)
`LocalStackDeployConfig` keeps `port`; drops `app_service_name` and
`container_name` (compose-specific naming knobs). Pulumi resource names are
derived from `app.name`, as AWS does.

In `_COMPUTE_CONFIGS`: replace `"local-compose"` with `"local-docker"`. Keep
`"local"` as the canonical key.

### Step 7 — Target registry and CLI

`deploy/targets/__init__.py` — register `local_docker` with name `"local"` and
aliases `("local-docker",)`.

[cli/deploy_cmd.py](../../artifacts/_skaal/skaal/cli/deploy_cmd.py) and
[cli/build_cmd.py](../../artifacts/_skaal/skaal/cli/build_cmd.py) — drop the
`--detach` and `--follow-logs` flags. Replace with the standard Pulumi `--yes`
flow already used by AWS/GCP.

### Step 8 — Tests

- Delete tests asserting compose YAML shape.
- Add `tests/deploy/test_local_stack_builder.py` mirroring the AWS one: build
  a stack from a fixture plan; assert the resulting dict has the expected
  `docker:Container` resources, env vars, `dependsOn`, ports, and network
  attachment.
- Add an integration test gated on `docker info` availability that runs the
  full build + `pulumi up --yes` against the `01_hello_world` example and
  curls `appUrl`.
- Update `dependency_sets.toml`: rename the `local-compose` set to
  `local-docker`.

### Step 9 — Dependencies

`pyproject.toml`: no Python dependency change. Pulumi itself is a CLI invoked
via subprocess. The `pulumi-docker` provider is auto-downloaded by `pulumi up`
from the `plugins` block in the generated `Pulumi.yaml`. Document the
`pulumi` CLI as a local prereq alongside Docker.

## Files touched

**Add**

- `deploy/builders/_local_stack_builder.py`
- `deploy/builders/local_stack.py` (re-export)
- `deploy/builders/_local_resources.py` (replaces `local_services.py`)
- `deploy/targets/local_docker.py`
- `tests/deploy/test_local_stack_builder.py`

**Modify**

- `deploy/pulumi/client.py` — `login_local`, passphrase env injection
- `deploy/builders/_gateways.py` — replace compose-service hooks with
  container-resource hooks
- `deploy/config.py` — trim `LocalStackDeployConfig`, rename alias
- `deploy/targets/__init__.py` — register the new target
- `deploy/data/dependency_sets.toml` — `local-compose` → `local-docker`
- `cli/deploy_cmd.py`, `cli/build_cmd.py` — drop compose-specific flags

**Delete**

- `deploy/builders/local_compose.py`
- `deploy/targets/local_compose.py`
- `deploy/local_services.py`
- `deploy/templates/local/docker-compose.yml`
- Compose-shape assertions in existing tests

## Order of execution

1. Step 5 — Pulumi-client local-backend plumbing (small, easy to test in
   isolation).
2. Steps 1–3 — build a `Pulumi.yaml` from a fixture plan; verify with unit
   tests before any deploy code runs.
3. Steps 4, 6, 7 — wire it into the target and CLI.
4. Step 8 — full test pass and one real `pulumi up` against
   `01_hello_world`.
5. Step 9 and cleanup — delete the compose code last, once the Pulumi path is
   green.

## Consequences

### Positive

- Single resource model and single deploy lifecycle across `local`, `aws`,
  `gcp`. Every future backend pays one cost, not two.
- `pulumi destroy` becomes the canonical local teardown — replaces the
  separate `docker compose down` semantics and brings parity with cloud
  targets.
- Future "real" local resources (custom processes, filesystem-backed
  services) can be added as Pulumi `dynamic:Resource` providers behind the
  same `_local_stack_builder` seam, instead of accreting one-off compose
  service entries.

### Negative

- **Slower local iteration**: `pulumi up` adds 2–5 s of preview/state overhead
  vs. `docker compose up`. Mitigated by the existing source mount + gunicorn
  `--reload`, so re-running `pulumi up` is rarely needed mid-edit.
- **Image rebuild noise**: `docker:Image` rebuilds whenever the build context
  hash changes. Acceptable; monitor for spurious rebuilds.
- **New local prereq**: contributors must install the `pulumi` CLI. Docker
  alone is no longer sufficient.

### Risks

- **Windows + Docker socket**: the `pulumi-docker` provider on Windows uses the
  named pipe by default. Should work with Docker Desktop but warrants one
  manual smoke test on a Windows dev machine before merge.
- **Stack state on disk**: `.pulumi-state/` is gitignored and treated as
  ephemeral. A corrupted state file is recovered by deleting the directory and
  running `pulumi up` again — document this in the local-runtime design doc.
