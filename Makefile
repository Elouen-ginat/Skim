.PHONY: install lint format typecheck test test-cov clean build dev

# ── Environment ────────────────────────────────────────────────────────────────
PYTHON  ?= python
PIP     ?= pip
PYTEST  ?= pytest
RUFF    ?= ruff
MYPY    ?= mypy

# ── Setup ──────────────────────────────────────────────────────────────────────
install:
	$(PIP) install -e ".[dev]"

dev: install
	@echo "Dev environment ready."

# ── Quality ────────────────────────────────────────────────────────────────────
lint:
	$(RUFF) check skaal tests examples

format:
	$(RUFF) format skaal tests examples

typecheck:
	$(MYPY) skaal

# ── Tests ──────────────────────────────────────────────────────────────────────
test:
	$(PYTEST) tests/ -q

test-cov:
	$(PYTEST) tests/ --cov=skaal --cov-report=term-missing -q

test-solver:
	$(PYTEST) tests/solver/ -q

test-storage:
	$(PYTEST) tests/storage/ -q

test-runtime:
	$(PYTEST) tests/runtime/ -q

test-schema:
	$(PYTEST) tests/schema/ -q

# ── Build ──────────────────────────────────────────────────────────────────────
build:
	maturin build --release

build-dev:
	maturin develop

# ── Cleanup ────────────────────────────────────────────────────────────────────
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	rm -rf dist/ build/ .coverage htmlcov/ .mypy_cache/ .ruff_cache/
