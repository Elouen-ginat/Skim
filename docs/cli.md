# Skaal CLI

## `skaal init`

Scaffold a new Skaal project.

```sh
skaal init demo            # creates ./demo/
skaal init demo --here     # writes into the current directory
skaal init demo --force    # overwrite existing files
```

The project name must be a valid Python identifier (letters, digits, `_`).

The generated layout:

```
demo/
├── pyproject.toml          # [tool.skaal] app = "demo.app:app"
├── README.md
├── .gitignore
├── catalogs/
│   └── local.toml          # editable copy of the bundled local catalog
└── demo/
    ├── __init__.py
    └── app.py              # counter starter app
```

Then:

```sh
cd demo
pip install -e .
skaal run
```

## `skaal run` hot-reload

`skaal run` watches your sources and restarts the server on save by default.

| Flag | Default | Effect |
| --- | --- | --- |
| `--reload` | — | Force reload on. |
| `--no-reload` | — | Force reload off (production-shape local runs). |
| (neither) | auto | On if stdout is a TTY and `SKAAL_ENV` is unset / `dev` / `local` / `development`. Off otherwise (CI, Docker, production). |
| `--reload-dir PATH` | project root | Repeat to watch additional roots. |

Reload uses subprocess supervision: the parent watches files, the child runs the actual server. This avoids uvicorn's in-process reload pitfalls with Skaal's plugin loaders.
