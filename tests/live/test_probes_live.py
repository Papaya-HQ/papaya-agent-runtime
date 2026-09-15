"""Opt-in live probe tests. Excluded from the default hermetic run.

Run with: `make test-live` (or `uv run pytest -m live`). These spawn the real
Claude/Codex CLIs in isolated temp repos and require authenticated harnesses.
"""

from __future__ import annotations

import tempfile

import pytest

from papaya_agent_runtime.probes.providers import default_models, detect_claude, detect_codex
from papaya_agent_runtime.probes.scenarios import run_provider

pytestmark = pytest.mark.live


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_provider_produces_capability_record(name: str) -> None:
    models = default_models()
    spec = (detect_claude if name == "claude" else detect_codex)(models[name])
    if spec is None:
        pytest.skip(f"{name} CLI not detected")

    with tempfile.TemporaryDirectory(prefix=f"ppy-live-{name}-") as evidence:
        record = run_provider(spec, evidence)

    assert record.provider == name
    assert record.cli_version
    # Fail-closed contract: booleans only, and at least the record is well formed.
    caps = record.capabilities.to_dict()
    assert all(isinstance(v, bool) for v in caps.values())
    assert record.scenarios, "expected at least one scenario result"
