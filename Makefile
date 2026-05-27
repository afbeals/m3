# m3 — Developer task runner
#
# Usage (macOS / Linux / Git Bash on Windows):
#   make setup          — create venv and install all dependencies
#   make test           — run the full test suite
#   make test-cov       — run tests with coverage report
#   make lint           — check code with ruff
#   make format         — auto-fix lint issues with ruff
#   make typecheck      — run mypy type checks
#   make dev            — start the app locally (scheduler + web dashboard)
#   make dev-reload     — start with DEBUG=true (uvicorn auto-reload)
#   make once           — run one metadata pass and exit
#   make validate       — validate all plugins in ./plugins/
#   make gen-test-lib   — generate a fake media library for testing
#   make clean          — remove venv, __pycache__, .coverage
#
# Windows alternative: use tasks.py (pure Python, no make required):
#   python tasks.py setup
#   python tasks.py test

PYTHON   ?= python3
VENV     := .venv
PIP      := $(VENV)/bin/pip
PYTEST   := $(VENV)/bin/pytest
RUFF     := $(VENV)/bin/ruff
MYPY     := $(VENV)/bin/mypy
PY       := $(VENV)/bin/python

# On Windows (Git Bash), bin/ is Scripts/
ifeq ($(OS),Windows_NT)
	PIP    := $(VENV)/Scripts/pip
	PYTEST := $(VENV)/Scripts/pytest
	RUFF   := $(VENV)/Scripts/ruff
	MYPY   := $(VENV)/Scripts/mypy
	PY     := $(VENV)/Scripts/python
endif

.PHONY: setup test test-cov lint format typecheck dev dev-reload once validate gen-test-lib clean help

help:
	@echo "Available targets:"
	@echo "  setup        Create venv and install all dependencies"
	@echo "  test         Run test suite"
	@echo "  test-cov     Run tests with coverage report"
	@echo "  lint         Check code with ruff"
	@echo "  format       Auto-fix lint issues"
	@echo "  typecheck    Run mypy"
	@echo "  dev          Start app (scheduler + web dashboard)"
	@echo "  dev-reload   Start app with uvicorn auto-reload (DEBUG=true)"
	@echo "  once         Run one metadata pass and exit"
	@echo "  validate     Validate all plugins in ./plugins/"
	@echo "  gen-test-lib Generate fake media library in ./test-media/"
	@echo "  clean        Remove venv and caches"

setup:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt -r requirements-dev.txt
	@echo ""
	@echo "Setup complete. Activate with:"
	@echo "  macOS/Linux:  source $(VENV)/bin/activate"
	@echo "  Windows CMD:  $(VENV)\\Scripts\\activate.bat"
	@echo "  PowerShell:   $(VENV)\\Scripts\\Activate.ps1"
	@echo ""
	@echo "Then copy .env.example to .env and fill in your values."

test:
	$(PYTEST) app/tests/ -v

test-cov:
	$(PYTEST) app/tests/ -v --cov=app --cov-report=term-missing

lint:
	$(RUFF) check app/

format:
	$(RUFF) check --fix app/

typecheck:
	$(MYPY) app/ --ignore-missing-imports

dev:
	$(PY) -m app.main

dev-reload:
	DEBUG=true $(PY) -m app.main

once:
	$(PY) -m app.main --once

validate:
	$(PY) -m app.main --validate-plugins

gen-test-lib:
	$(PY) scripts/generate_test_library.py

clean:
	rm -rf $(VENV) __pycache__ app/__pycache__ .coverage coverage.xml
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
