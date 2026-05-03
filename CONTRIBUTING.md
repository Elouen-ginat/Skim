# Contributing to Skaal

Thank you for your interest in contributing to Skaal! This document provides guidelines for contributing to the project.

## Development Setup

### 1. Clone and Install

```bash
git clone https://github.com/Elouen-ginat/Skaal.git
cd Skaal

# Install with dev dependencies
pip install -e ".[dev]"
# or with uv:
uv sync --group dev
```

### 2. Set Up Pre-commit Hooks

Pre-commit hooks ensure code quality before each commit:

**macOS/Linux:**
```bash
bash scripts/setup-pre-commit.sh
```

**Windows:**
```bash
scripts\setup-pre-commit.bat
```

**Manual:**
```bash
pre-commit install
```

See [PRE_COMMIT_SETUP.md](PRE_COMMIT_SETUP.md) for detailed information.

### 3. Verify Setup

```bash
# Run all hooks on your changes
pre-commit run --all-files

# Run tests
pytest

# Type checking
mypy skaal tests

# Linting
ruff check skaal tests
```

### Mesh Development

Most contributors do not need Rust anymore. `uv sync` and `uv sync --group dev`
set up the Python-only development environment.

If you also need the distributed mesh runtime from a local checkout:

```bash
uv sync --group dev --extra mesh
```

Before the mesh wheels are published for a release tag, that extra resolves to
the local `mesh/` crate and therefore needs a Rust toolchain.

If you are editing code under `mesh/`:

```bash
# Rebuild the local extension into the active environment
make build-dev

# Build release wheels locally before pushing
make build
```

That local editable build shadows the published wheel in your current virtualenv.

For `skaal build --target local --dev`, Skaal now bundles a prebuilt Linux
`skaal-mesh` wheel into the Docker artifact when both of these are true:

```toml
[tool.skaal]
enable_mesh = true
```

- A compatible `manylinux` wheel exists under `target/wheels/` or `mesh/dist/`
- The wheel matches the Docker image architecture (`x86_64` or `aarch64`)

On Windows, `maturin build` produces a Windows wheel, which Docker cannot use
inside the Linux runtime image. For local Docker mesh testing, download or
build a Linux `manylinux` wheel first, then run `skaal build --target local --dev`.

## Workflow

1. Create a feature branch: `git checkout -b feature/my-feature`
2. Make changes (pre-commit hooks will run on commit)
3. Write tests for new functionality
4. Run tests: `pytest`
5. Push and create a Pull Request

## Code Standards

### Python

- Use **ruff** for linting and formatting (automatically enforced by pre-commit)
- Use **mypy** for static type checking (included in pre-commit)
- Write unit tests in `tests/` directory
- Follow PEP 8 style guidelines

### YAML/JSON

- Validate with pre-commit hooks (automatically enforced)
- Use 2-space indentation

### Commits

- Use descriptive commit messages
- Reference issues when applicable: "Fixes #123"
- Pre-commit will auto-fix formatting issues before commit

## Running Tests

```bash
# All tests
pytest

# With coverage
pytest --cov=skaal

# Specific test file
pytest tests/solver/test_solver.py

# Specific test function
pytest tests/solver/test_solver.py::test_basic_solver
```

## Pre-commit Hooks

Hooks run automatically on `git commit` and include:
- **Ruff**: Python linting & formatting
- **MyPy**: Type checking
- **YAML/JSON validators**: Format validation
- **Trailing whitespace**: Auto-fix
- **Security checks**: Bandit

To run manually:
```bash
pre-commit run --all-files
```

To skip hooks (not recommended):
```bash
git commit --no-verify
```

## Documentation

- Update [LOCAL_DEPLOYMENT.md](LOCAL_DEPLOYMENT.md) if adding deployment features
- Update docstrings for public APIs
- Add type hints to new functions

## Reporting Bugs

Please use GitHub Issues and include:
- Description of the bug
- Steps to reproduce
- Expected behavior
- Actual behavior
- OS/Python version info

## Feature Requests

Use GitHub Issues with:
- Clear description of the feature
- Why you think it would be useful
- Possible implementation approach (optional)

## Pull Requests

- Clear PR title and description
- Link to related issues
- All tests pass
- Code passes pre-commit checks
- Documentation updated if needed

## Questions?

Have questions about contributing? Open an issue or check existing documentation:
- [PRE_COMMIT_SETUP.md](PRE_COMMIT_SETUP.md) - Pre-commit hook details
- [LOCAL_DEPLOYMENT.md](LOCAL_DEPLOYMENT.md) - Deployment and testing
- [pyproject.toml](pyproject.toml) - Project configuration

---

**Thank you for contributing to Skaal!** 🎉
