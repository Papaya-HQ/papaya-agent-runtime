"""Hermetic tests for the interactive manager launcher (`ppy start`)."""

from __future__ import annotations

import os

import pytest

from papaya_agent_runtime.cli import main
from papaya_agent_runtime.config import ManagerProfile, MMConfig, WorkerCeiling
from papaya_agent_runtime.manager import ManagerLaunchError, build_launch


def _cfg(provider="claude", model="opus", reasoning="high") -> MMConfig:
    return MMConfig(
        manager=ManagerProfile(provider=provider, model=model, reasoning=reasoning),
        worker=WorkerCeiling(provider="codex", max_model="gpt-5-codex", max_reasoning="medium"),
    )


def test_claude_launch_injects_role_path_and_seed() -> None:
    launch = build_launch(config=_cfg(), root="/repo", base_env={"PATH": "/usr/bin"})
    assert launch.provider == "claude"
    assert launch.argv[0] == "claude"
    assert launch.argv[:3] == ["claude", "--model", "opus"]
    assert "--append-system-prompt" in launch.argv
    # ppy wrapper is pre-allowed so the manager can drive the control plane.
    assert "Bash(ppy:*)" in launch.argv
    # bin/ is prepended to PATH so `ppy` resolves to the repo wrapper.
    assert launch.env["PATH"].startswith("/repo/bin" + os.pathsep)
    assert launch.env["PPY_MANAGER_SESSION"] == "1"
    # The seed prompt (last arg) points at the runtime contract.
    assert "docs/runtime-contract.md" in launch.argv[-1]


def test_codex_launch_uses_reasoning_and_seed() -> None:
    launch = build_launch(config=_cfg(provider="codex", model="gpt-5-codex", reasoning="medium"))
    assert launch.provider == "codex"
    assert launch.argv[0] == "codex"
    assert "--model" in launch.argv
    # Codex reasoning is passed as a config override.
    assert "model_reasoning_effort=medium" in launch.argv
    assert "docs/runtime-contract.md" in launch.argv[-1]


def test_provider_override_does_not_inherit_mismatched_model() -> None:
    # Config manager is claude/opus, but the user enters as codex; opus must not leak.
    launch = build_launch(config=_cfg(provider="claude", model="opus"), provider="codex")
    assert launch.provider == "codex"
    assert "opus" not in launch.argv
    assert launch.model is None  # falls back to the harness default


def test_explicit_overrides_win() -> None:
    launch = build_launch(config=_cfg(), provider="claude", model="haiku", reasoning="low")
    assert launch.model == "haiku"
    assert launch.argv[:3] == ["claude", "--model", "haiku"]


def test_objective_is_seeded() -> None:
    launch = build_launch(config=_cfg(), objective="add a health endpoint")
    assert "add a health endpoint" in launch.seed_prompt
    assert launch.seed_prompt == launch.argv[-1]


def test_unconfigured_seed_walks_through_setup() -> None:
    launch = build_launch(config=None, provider="claude")
    assert "isn't configured yet" in launch.seed_prompt
    assert "ppy setup" in launch.seed_prompt
    # Preflight self-bootstrap is seeded for the unconfigured path.
    assert "./bin/install" in launch.seed_prompt


def test_configured_seed_runs_preflight() -> None:
    launch = build_launch(config=_cfg())
    assert "preflight" in launch.seed_prompt.lower()


def test_no_provider_and_no_config_raises() -> None:
    with pytest.raises(ManagerLaunchError):
        build_launch(config=None, provider=None)


def test_cli_dry_run_prints_invocation(capsys) -> None:
    rc = main(["start", "--provider", "claude", "--dry-run", "ship the thing"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "provider: claude" in out
    assert "argv:" in out
    assert "ship the thing" in out
