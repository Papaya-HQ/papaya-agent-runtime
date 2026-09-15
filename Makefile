# Papaya Agent Runtime control-plane developer targets.
# All Python runs go through uv, respecting versions pinned by pyproject/uv.lock.

.PHONY: help fmt lint test test-live probe clean

help:
	@echo "Targets:"
	@echo "  make fmt        Format with ruff"
	@echo "  make lint       Lint with ruff"
	@echo "  make test       Run hermetic tests (no provider spawning)"
	@echo "  make test-live  Run opt-in live probe tests (spawns Claude/Codex)"
	@echo "  make probe      Run the live interrupt/resume probe matrix into .ppy/provider-capabilities.json (machine-local)"

fmt:
	uv run ruff format .

lint:
	uv run ruff check .

test:
	uv run pytest

test-live:
	uv run pytest -m live

# Live probes: spawn real Claude/Codex CLIs in isolated temp repos and record
# a fail-closed capability matrix. Requires authenticated harnesses. Writes the
# machine-local record only; add --publish to refresh the tracked files.
# PYTHONPATH mirrors bin/ppy: the package is not installed into the venv, it is
# imported from src/. PPY_HOME pins the machine-local record to this checkout.
probe:
	PYTHONPATH=src PPY_HOME=$(CURDIR)/.ppy uv run python -m papaya_agent_runtime.probes run $(ARGS)

clean:
	rm -rf .venv .pytest_cache **/__pycache__
