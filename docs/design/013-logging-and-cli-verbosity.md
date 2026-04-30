# ADR 013 — Logging and CLI Verbosity

**Status:** Proposed
**Date:** 2026-04-29
**Related:** [ADR 012](012-pulumi-automation-and-docker-py-refactor.md)

## Context

Skaal has no logging infrastructure. User-facing output is split across
three styles, with no shared configuration:

| Style                      | Where                                                  | Purpose                          |
| -------------------------- | ------------------------------------------------------ | -------------------------------- |
| `typer.echo(...)`          | every `skaal/cli/*_cmd.py`                             | CLI status, error reporting      |
| `print(...)`               | `skaal/runtime/local.py`, `runtime/mesh_runtime.py`    | Server banners, schedule errors  |
| `warnings.warn(...)`       | `skaal/solver/solver.py`                               | Solver fallbacks and overrides   |
| `print(f"==> ...")`        | `skaal/deploy/packaging/local.py` (and prior `cli.py`) | Build progress markers           |

Concrete consequences:

- **No way to silence any of it.** A user calling `api.deploy(...)` from
  their own Python program inherits an unsolicited `==> Building local app
  image ...` print on stdout.
- **No way to amplify any of it.** When deploys fail, the only debug signal
  is to add `print` statements and re-run; the underlying Pulumi/docker-py
  events that ADR 012 introduces have nowhere structured to go.
- **No structured fields.** Errors print `Error: {exc}` and lose stack
  context. CI logs are unsearchable for app/stack/target identifiers.
- **CLI and library are conflated.** `skaal.deploy.targets.registry`
  imports `typer` and calls `typer.echo` from inside `package_and_push` —
  library code with a CLI dependency. ADR 012's `AutomationRunner` would
  inherit the same anti-pattern unless we fix the seam now.
- **No verbosity flag.** The root CLI has no global `--verbose` / `--quiet`
  / `-v -v`. Each command improvises.

## Decision

Introduce a single `logging` hierarchy rooted at the `skaal` logger,
configure it once at the CLI entry point, and route every existing output
site through it. The Python API exposes the same hierarchy as a public
contract: callers attach their own handlers to `logging.getLogger("skaal")`
and get the full event stream without any Skaal-specific configuration API.

The CLI gains a global `--verbose / -v` (repeatable), `--quiet / -q`, and
`--log-format {text,json}` set on the root Typer callback. Library code
never imports `typer`, never calls `print`, and never configures logging.

## Why `logging`, not Rich/structlog/loguru directly

- Standard-library `logging` is the only sink every downstream user already
  knows how to integrate with. Frameworks (Django, FastAPI, Airflow,
  Lambda) all pre-configure it; users who embed `skaal` get sensible
  defaults for free.
- `LogRecord.extra` carries structured fields without committing to a
  serialisation library. We can render text in the CLI today and add a JSON
  formatter later (ADR amendment) without touching call sites.
- Rich is allowed as the *CLI handler's formatter*, not as the API. The
  library never imports `rich`; only `skaal/cli/_logging.py` does.

## Logger Hierarchy

One logger per subsystem, all under `skaal.*`:

| Logger name              | Owner                                        |
| ------------------------ | -------------------------------------------- |
| `skaal`                  | root — never log directly                    |
| `skaal.cli`              | CLI command bodies                           |
| `skaal.solver`           | constraint solver, replaces `warnings.warn`  |
| `skaal.plan`             | plan loading / writing                       |
| `skaal.deploy`           | deploy facade, target dispatch               |
| `skaal.deploy.pulumi`    | `AutomationRunner` lifecycle                 |
| `skaal.deploy.docker`    | docker-py builder/pusher                     |
| `skaal.deploy.packaging` | Lambda packaging                             |
| `skaal.runtime`          | local + mesh runtimes (server banners, schedule errors) |
| `skaal.api`              | public Python entry points                   |
| `skaal.mesh`             | mesh client                                  |

Sub-loggers inherit handlers from `skaal`. Library code obtains its logger
once at module top:

```python
log = logging.getLogger(__name__)   # implicit "skaal.deploy.pulumi.runner"
```

## Levels

The standard five levels with explicit conventions:

| Level    | Use                                                                                |
| -------- | ---------------------------------------------------------------------------------- |
| `ERROR`  | Operation failed; exception is being raised. One per failure, at the catch site.   |
| `WARNING`| Recoverable surprise (solver fallback, default region applied, deprecated input).  |
| `INFO`   | Single-line milestones a non-debug user wants to see ("Building image …", "Stack `dev` selected", "App URL: …"). |
| `DEBUG`  | Mechanism: arguments passed to Pulumi, env-var resolution, retry attempts, image IDs. |
| `TRACE`  | Not used. (Custom level deferred — `DEBUG` is sufficient for now.)                 |

The `==>` prefix style currently used by deploy is an `INFO` formatter
concern, not a level concern. The CLI's text formatter renders `INFO`
records from `skaal.deploy.*` with `==>` so the on-screen experience does
not regress.

## Streaming output (Pulumi + Docker)

ADR 012 introduces `AutomationRunner.stack.up(on_output=…)` and a docker-py
log stream. Both produce *non-record* output: a continuous byte stream from
a child engine. They are wrapped in a small adapter so they appear as
`logging` records:

```python
# skaal/deploy/_progress.py
class ProgressSink:
    """Routes Pulumi engine stdout and docker build events to logging."""
    def __init__(self, logger: logging.Logger):
        self._log = logger

    def pulumi_output(self, line: str) -> None:
        self._log.info(line.rstrip(), extra={"source": "pulumi"})

    def pulumi_event(self, event: dict) -> None:
        self._log.debug("pulumi.event", extra={"source": "pulumi", "event": event})

    def docker_log(self, chunk: dict) -> None:
        if "stream" in chunk:
            self._log.info(chunk["stream"].rstrip(), extra={"source": "docker"})
        elif "error" in chunk:
            self._log.error(chunk["error"], extra={"source": "docker"})
        else:
            self._log.debug("docker.event", extra={"source": "docker", "event": chunk})
```

This is the only place Pulumi/Docker stream output crosses into the Python
logger, so per-stream verbosity becomes a `setLevel` call on
`skaal.deploy.pulumi` / `skaal.deploy.docker`.

## CLI configuration

A new module `skaal/cli/_logging.py` owns CLI handler setup. The root
callback in `skaal/cli/main.py` adds three global options:

```python
@app.callback()
def _root(
    verbose: int = typer.Option(0, "--verbose", "-v", count=True,
        help="Increase log verbosity. -v=INFO, -vv=DEBUG."),
    quiet: bool = typer.Option(False, "--quiet", "-q",
        help="Suppress INFO logs. Errors still print."),
    log_format: str = typer.Option("text", "--log-format",
        help="text or json. Env: SKAAL_LOG_FORMAT."),
) -> None:
    configure_cli_logging(verbose=verbose, quiet=quiet, fmt=log_format)
```

Resolution order (highest priority first):

1. CLI flag (`--verbose`, `--quiet`, `--log-format`)
2. `SKAAL_LOG_LEVEL` / `SKAAL_LOG_FORMAT` env vars
3. `[tool.skaal.logging]` in `pyproject.toml` (level, format, per-logger overrides)
4. Default: `WARNING` for `skaal`, `INFO` for `skaal.cli` and
   `skaal.deploy`, errors only for everything else.

`-v` and `-vv` raise the floor for *all* `skaal.*` loggers; `--quiet` sets
the floor to `ERROR`. Per-logger overrides from `pyproject.toml` always
apply on top, so a user can keep `skaal.solver` at `INFO` while running
`-vv`.

### Text formatter (default)

```
12:04:11 INFO   skaal.deploy        ==> Building local app image
12:04:11 INFO   skaal.deploy.docker Step 1/8 : FROM python:3.11-slim
12:04:14 INFO   skaal.deploy.pulumi Updating (dev): + 4 to add
12:04:25 INFO   skaal.cli           App URL: http://localhost:8080
```

- Records from `skaal.cli` print without the logger column (clean status
  lines).
- Records from `skaal.deploy.*` get the `==>` prefix at `INFO`.
- `WARNING`/`ERROR` records are coloured via Rich if the handler stream is
  a TTY; otherwise plain.

### JSON formatter

One JSON object per record on stderr, suitable for CI ingestion:

```json
{"ts":"2026-04-29T12:04:14Z","level":"INFO","logger":"skaal.deploy.pulumi","msg":"Updating (dev): + 4 to add","app":"task-dashboard","stack":"dev","target":"local"}
```

`extra={"app": ..., "stack": ..., "target": ...}` is set once per deploy by
the runner; the JSON formatter promotes these to top-level keys.

## Library API contract

The Python API exposes logging as a *capability*, not as a configuration
function. Two guarantees:

1. **No handler is attached by default.** `import skaal` does not call
   `logging.basicConfig`, does not add a `StreamHandler` to `skaal`, and
   does not set any level. `logging.getLogger("skaal")` returns the
   bare logger; the embedding application owns presentation.
2. **A `NullHandler` is attached at package load.** Standard library
   guidance — prevents "No handlers could be found for logger" warnings on
   Python <3.2 stdlib paths but, more importantly, makes it explicit that
   the library will log into whatever handler the host has configured.

Documented usage:

```python
import logging
logging.basicConfig(level=logging.INFO)        # or attach your own handler
logging.getLogger("skaal.deploy").setLevel(logging.DEBUG)

from skaal import api
api.deploy(...)                                # logs flow to your handler
```

`skaal.api` does not gain a `verbose=` or `log_level=` kwarg. Verbosity is
not part of the deploy contract; it is a sink-side concern.

### What gets logged where

| Today                                                       | After                                    |
| ----------------------------------------------------------- | ---------------------------------------- |
| `typer.echo("Generating artifacts in {out}/")` in `build_cmd.py` | `log.info("Generating artifacts in %s", out)` from `skaal.api.build` |
| `typer.echo(f"Error: {exc}", err=True); raise typer.Exit(1)` | `log.exception("…")` at the catch site, CLI maps exception → exit code |
| `typer.echo(f"\nApp URL: {url}")` in `targets/registry.py`   | `log.info("App URL: %s", url, extra={...})` from `skaal.deploy` |
| `print(f"\n  Skaal local runtime …")` banners in `runtime/local.py` | `log.info(...)` on `skaal.runtime`; the CLI's `skaal run` formatter renders the banner block |
| `warnings.warn(...)` in `skaal/solver/solver.py`            | `log.warning(...)` on `skaal.solver`. `warnings.warn` is reserved for true API deprecations. |
| `print(f"==> Docker build context: …")` in `packaging/local.py` | `log.info(...)` on `skaal.deploy.docker` (via `ProgressSink`) |

The `print` calls inside docstrings (`skaal/module.py:529`,
`skaal/schedule.py:161`, `skaal/mesh/__init__.py`) are not output sites;
they stay as illustrative documentation.

## Error reporting in the CLI

Library code raises typed exceptions; the CLI catches them in one place and
maps them to exit codes + a single `log.error` line. The current pattern of

```python
except FileNotFoundError as exc:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(1) from exc
except ValueError as exc:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(1) from exc
except Exception as exc:
    typer.echo(f"Deploy failed: {exc}", err=True)
    raise typer.Exit(1) from exc
```

…repeats in five command files. Replace with a single decorator in
`skaal/cli/_errors.py`:

```python
@cli_error_boundary
def deploy(...): ...
```

The decorator catches `SkaalError` (new — base class for user-facing
domain errors), the `DeployError` from ADR 012, `FileNotFoundError`,
`ValueError`, and unhandled `Exception`. Maps each to the right exit code
and emits one `log.error` (with `exc_info=True` at `DEBUG`) — no
`typer.echo`.

## Exception class

```python
# skaal/errors.py  (new)
class SkaalError(Exception):
    """Base class for all user-facing skaal exceptions."""
    exit_code: int = 1

class PlanError(SkaalError): ...
class BuildError(SkaalError): ...
class DeployError(SkaalError): ...   # also referenced by ADR 012
class CatalogError(SkaalError): ...
```

Exception messages are user-facing and must be self-contained. The CLI
decorator prints `log.error(str(exc))`; with `-v` it adds `exc_info=True`
to surface tracebacks. Library callers can catch `SkaalError` for the
whole hierarchy or specific subclasses.

## Migration Steps

This is a direct cutover (per ADR 011 conventions) but spans more files
than ADR 012. Land in three PRs:

### PR 1 — Plumbing

1. Add `skaal/_logging.py` with `NullHandler` attachment for the `skaal`
   root logger; import it from `skaal/__init__.py`.
2. Add `skaal/errors.py` with `SkaalError` hierarchy.
3. Add `skaal/cli/_logging.py` with `configure_cli_logging(...)` and the
   text/JSON formatters (Rich-aware on TTY).
4. Add `skaal/cli/_errors.py` with the `cli_error_boundary` decorator.
5. Wire `--verbose / -v / -vv`, `--quiet / -q`, and `--log-format` into
   `skaal/cli/main.py`'s root callback.

No behaviour change yet for callers that don't pass new flags — defaults
preserve current INFO-level output.

### PR 2 — Migrate output sites

6. Replace every `typer.echo` in `skaal/cli/*_cmd.py` with `log.info` /
   `log.error`. Apply the `cli_error_boundary` decorator to each command
   body; delete the per-command try/except cascade.
7. Replace `typer.echo` calls inside library code
   (`skaal/deploy/targets/registry.py`, `skaal/deploy/pulumi/automation.py`)
   with `log.info` on the appropriate `skaal.deploy.*` logger. Remove the
   `typer` import from library modules.
8. Replace `print(...)` in `skaal/runtime/local.py` and
   `runtime/mesh_runtime.py` with `log.info` on `skaal.runtime`. The
   server-banner block becomes a single multi-line `INFO` record so the
   CLI formatter can render it as a coherent block.
9. Replace `warnings.warn(...)` in `skaal/solver/solver.py` with
   `log.warning(...)` on `skaal.solver`, *except* for the messages that
   document deprecated public-API usage — those stay as `warnings.warn`
   with `DeprecationWarning`.

### PR 3 — Streaming integration (depends on ADR 012)

10. Add `skaal/deploy/_progress.py:ProgressSink`.
11. Wire `AutomationRunner` (ADR 012) to take a `ProgressSink` and pass
    `sink.pulumi_output` / `sink.pulumi_event` as `on_output` /
    `on_event` callbacks.
12. Wire `docker_builder.build_image` to call `sink.docker_log` for each
    decoded log chunk.
13. Construct the `ProgressSink` from `logging.getLogger("skaal.deploy")`
    inside `targets/registry.py` so the streaming output respects the
    same level configuration as everything else.

## Tests

- **CLI verbosity behaviour:** `tests/cli/test_logging.py` exercises the
  `-v` / `-vv` / `-q` flag matrix using Typer's `CliRunner` and asserts on
  captured stderr.
- **Library quiet by default:** `tests/test_library_silent.py` imports
  `skaal`, calls a no-op `api.build` with a fake plan, and asserts no
  output reaches stdout/stderr unless a handler is attached.
- **Per-logger override from pyproject:** `tests/cli/test_logging.py` adds
  a fixture pyproject with `[tool.skaal.logging] level = "DEBUG"` and
  asserts `DEBUG` records emerge.
- **JSON formatter shape:** golden test for one record per line, required
  fields `ts`, `level`, `logger`, `msg`.
- Existing tests that capture `typer.echo` output via `CliRunner.stdout`
  must move to capturing the logger via `caplog`. This is the largest
  test diff but mechanical.

## Non-Goals

- Replacing `warnings.warn` for `DeprecationWarning` usage. Deprecations
  remain `warnings.warn` so they trigger `pytest -W error::DeprecationWarning`.
- Distributed tracing / OpenTelemetry. The `extra={"app", "stack",
  "target"}` carrier is forward-compatible with OTel spans, but adding
  exporters is out of scope.
- A custom `TRACE` level. `DEBUG` is sufficient; revisit only if Pulumi
  engine event streams overwhelm `DEBUG`.
- Log file rotation. The CLI writes to stderr; users compose with shell
  redirection or systemd's journal.
- Coloured output as a contract. Rich is used for TTY rendering only;
  scripts that parse stderr should pass `--log-format json`.

## Open Questions

- **Schedule errors in `runtime/local.py`** currently `print(...)` on
  every misfire. `INFO` or `WARNING`? Decision: `WARNING`, since a
  schedule firing failure is a user-visible recoverable event.
- **Lambda packaging `pip install -t` output.** The subprocess (kept per
  ADR 012) writes to stdout. Capture via `subprocess.PIPE` and forward
  per line to `skaal.deploy.packaging`, or let it pass through? Decision
  pending; default is to forward, since pass-through breaks `--quiet`.
- **Per-app log directories.** Should `skaal deploy` write a structured
  log to `artifacts/.skaal-logs/<timestamp>.json` regardless of CLI
  level? Useful for post-mortem on cloud deploys but doubles I/O. Defer.
