# ChenDB — development tasks.
#
# The engine has no runtime dependencies; `make test-engine` runs against a
# bare interpreter. Everything else needs the venv created by `make install`.

PYTHON  ?= python3
VENV    ?= .venv
BIN     := $(VENV)/bin
NPM     := npm --prefix visualizer

.DEFAULT_GOAL := help
.PHONY: help install engine server ui test test-engine test-api test-ui \
        lint typecheck bench example types clean

help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Create the venv and install everything
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install -q --upgrade pip
	$(BIN)/pip install -q -e '.[server,dev]'
	$(NPM) install
	@echo "Ready. Try: make engine"

engine:  ## Interactive storage explorer (no dependencies needed)
	$(PYTHON) -m engine demo.chendb

server:  ## Start the HTTP + WebSocket API on 127.0.0.1:8000
	$(BIN)/python -m engine.server --workspace workspace

ui:  ## Start the visualizer on localhost:5173
	$(NPM) run dev

test: test-engine test-ui  ## Run every test

test-engine:  ## Python tests (engine, API, recovery)
	$(BIN)/python -m pytest -q

test-api:  ## API and WebSocket tests only
	$(BIN)/python -m pytest -q tests/integration

test-ui:  ## Frontend component tests
	$(NPM) test

lint:  ## Ruff over the Python sources
	$(BIN)/ruff check engine tests benchmarks examples scripts
	$(BIN)/ruff format --check engine tests benchmarks examples scripts

typecheck:  ## TypeScript typecheck
	$(NPM) run typecheck

bench:  ## Measure diagnostics and storage overhead
	$(BIN)/python benchmarks/trace_overhead.py

example:  ## Narrated walkthrough of the storage engine
	$(BIN)/python examples/milestone1_storage.py

types:  ## Regenerate TypeScript types from the OpenAPI schema
	$(BIN)/python scripts/generate_api_types.py

coverage:  ## Python tests with a coverage report
	$(BIN)/python -m pytest -q --cov=engine --cov-report=term-missing

clean:  ## Remove build artefacts and generated databases
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov visualizer/dist
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	find . -name '*.chendb' -delete
