# Skaal

**Infrastructure as Constraints** — write your app once, scale it with a word.

Skaal lets you describe your application's storage and compute needs as constraints, then automatically selects the right infrastructure backend (local SQLite, Redis, PostgreSQL, AWS DynamoDB, GCP Firestore, …) that satisfies those constraints for your target environment.

## Quickstart

```bash
pip install "skaal[serve]"
skaal init demo
cd demo && pip install -e .
skaal run                       # hot-reloads on save
```

```bash
# Distributed mesh runtime (prebuilt wheel)
pip install "skaal[mesh]"
```

See `docs/cli.md` for `skaal init` and `skaal run` reload flags.

Install `skaal[runtime]` as well when your app uses schedules, JWT auth, or OpenTelemetry.

## HTTP APIs

Skaal treats `@app.function()` as compute plus resilience, not as your public router. Mount FastAPI, Starlette, or Litestar with `app.mount_asgi(...)`, then call Skaal compute through `await app.invoke(...)` or `app.invoke_stream(...)` from your handlers.

See `docs/http.md` for the supported pattern. The runnable examples are `examples.todo_api:app` for CRUD, `examples.fastapi_streaming:app` for SSE, and `examples.file_upload_api:app` for FastAPI file uploads backed by the blob tier.

## Blob Storage

Skaal includes a first-class blob/object storage surface for file workloads. Declare a `BlobStore` with `@app.blob(...)`, then use `put_bytes`, `put_file`, `get_bytes`, `download_file`, `stat`, and `list_page`.

```python
from skaal import App, BlobStore

app = App("uploads")


@app.blob(read_latency="< 500ms", durability="durable")
class Uploads(BlobStore):
    pass


async def save_avatar(filename: str, payload: bytes) -> None:
    await Uploads.put_bytes(f"avatars/{filename}", payload, content_type="image/png")
```

Use `catalogs/local.toml` for local filesystem-backed blobs, `catalogs/aws.toml` for S3, and `catalogs/gcp.toml` for GCS.

## Cloud Deployment

```bash
# Deploy to AWS
pip install "skaal[deploy,aws]"
skaal deploy --app myapp:app --target aws --catalog catalogs/aws.toml

# Deploy to GCP
pip install "skaal[deploy,gcp]"
skaal deploy --app myapp:app --target gcp --catalog catalogs/gcp.toml
```

## How It Works

1. **Annotate** your modules with resource constraints (`@storage`, `@compute`, `@scale`, …)
2. **Plan** — the Z3 SMT solver picks the cheapest backend satisfying all constraints from your catalog
3. **Build** — Skaal generates Dockerfiles, Pulumi programs, and handler entrypoints
4. **Deploy** — push to your cloud or run locally with a single command

## Installation

| Extra | Installs |
|-------|----------|
| `skaal` | Core planning, solver, storage APIs, local data backends |
| `skaal[serve]` | + local HTTP serving (`skaal run`, ASGI/WSGI mounting) |
| `skaal[runtime]` | + schedules, JWT auth, OpenTelemetry runtime hooks |
| `skaal[deploy]` | + Docker and Pulumi deployment tooling |
| `skaal[aws]` | + AWS storage/runtime provider packages |
| `skaal[gcp]` | + GCP storage/runtime provider packages |
| `skaal[fastapi]` | + FastAPI and multipart upload support |
| `skaal[dash]` | + Dash UI dependencies |
| `skaal[examples]` | + all bundled example app dependencies |
| `skaal[mesh]` | + prebuilt `skaal-mesh` wheel for the distributed runtime |
| `skaal[examples]` | + Dash, FastAPI, dash-bootstrap-components |

## License

GPL-3.0-or-later
