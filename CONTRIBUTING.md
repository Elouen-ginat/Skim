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
