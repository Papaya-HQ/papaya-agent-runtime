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


def test_a_turn_is_the_same_manager_built_headless() -> None:
    """`ppy serve`'s turns come from this builder, not a second launcher."""
    claude = build_launch(config=_cfg(), turn="# Turn: review", base_env={"PATH": "/usr/bin"})
    # The prompt sits right after `-p`: `--allowedTools` is variadic and would
    # otherwise read a trailing prompt as one more tool name.
    assert claude.argv[:3] == ["claude", "-p", "# Turn: review"]
    assert claude.argv[-1] == "Bash(./bin/ppy:*)"
    assert "--append-system-prompt" in claude.argv
    assert claude.seed_prompt == "# Turn: review"
    assert claude.env["PPY_MANAGER_SESSION"] == "1"

    codex = build_launch(config=_cfg(provider="codex", model="gpt-5-codex"), turn="# Turn: brief")
    assert codex.argv[:2] == ["codex", "exec"]
    # Codex has no system-prompt flag, so the role rides ahead of the turn in the one
    # prompt it gets (#74): the same contract pointer a Claude turn has in its system
    # prompt, and the turn's heading still the first line after it.
    assert codex.argv[-1] == codex.seed_prompt
    assert codex.seed_prompt.endswith("\n\n---\n\n# Turn: brief")
    assert codex.seed_prompt.count("# Turn: brief") == 1
    for launch in (claude, codex):
        carried = " ".join(launch.argv)
        assert "docs/runtime-contract.md" in carried, launch.provider
        assert "overrides any repository-development instructions" in carried, launch.provider
        assert "never ask the user to run a command" in carried, launch.provider
    # An interactive Codex session still gets the seed prompt alone, as before.
    assert build_launch(config=_cfg(provider="codex")).seed_prompt.startswith("Starting a")


def test_a_codex_turn_gets_the_clients_mcp_overrides_the_way_its_runner_does(tmp_path) -> None:
    """`papaya-codex-runner.sh`'s shape: one `-c` per `runner-config` line, and `--cd`."""
    import subprocess

    from papaya_agent_runtime.manager import prepare_turn_tools

    seen: list[list[str]] = []

    def run(command, **_kwargs):
        seen.append(command)
        out = 'mcp_servers.papaya.enabled=false\nmcp_servers.papaya-job.url="https://x"\n'
        return subprocess.CompletedProcess(command, 0, stdout=out, stderr="")

    env = {"PAPAYA_AGENT_BIN": "/opt/papaya-agent", "PAPAYA_AGENT_REF": "@tester"}
    tools = prepare_turn_tools(
        "codex", env, root=str(tmp_path), config_file=tmp_path / "unused.json", run=run
    )
    assert seen == [
        [
            "/opt/papaya-agent",
            *("mcp", "runner-config", "--harness", "codex", "--agent", "@tester"),
            *("--working-directory", str(tmp_path)),
        ]
    ]
    codex = build_launch(
        config=_cfg(provider="codex", model="gpt-5-codex"),
        turn="# Turn: brief",
        root=str(tmp_path),
        tools=tools,
    )
    assert codex.argv[:6] == [
        "codex",
        "exec",
        "-c",
        "mcp_servers.papaya.enabled=false",
        "-c",
        'mcp_servers.papaya-job.url="https://x"',
    ]
    assert codex.argv[-3:-1] == ["--cd", str(tmp_path)]
    assert codex.argv[-1].endswith("# Turn: brief")

    def refused(command, **_kwargs):
        return subprocess.CompletedProcess(command, 75, stdout="", stderr="identity mismatch\n")

    with pytest.raises(ManagerLaunchError, match="identity mismatch"):
        prepare_turn_tools(
            "claude", env, root=str(tmp_path), config_file=tmp_path / "c.json", run=refused
        )


def test_run_turn_captures_the_transcript_and_the_exit_code(tmp_path) -> None:
    import sys

    from papaya_agent_runtime.manager import Launch, run_turn

    launch = Launch(
        argv=[sys.executable, "-c", "import sys; print('dispatched task 4'); sys.exit(3)"],
        env=dict(os.environ),
        cwd=str(tmp_path),
        provider="claude",
    )
    result = run_turn(launch)
    assert result.exit_code == 3
    assert "dispatched task 4" in result.transcript
    assert result.tail(7) == "task 4\n"
    assert result.stopped is False


def test_run_turn_ends_the_harness_when_the_hold_stops(tmp_path, monkeypatch) -> None:
    import sys

    from papaya_agent_runtime.manager import Launch, run_turn
    from papaya_agent_runtime.manager import launch as launch_module

    monkeypatch.setattr(launch_module, "TURN_STOP_POLL_SECONDS", 0.05)
    launch = Launch(
        argv=[sys.executable, "-c", "import time; time.sleep(60)"],
        env=dict(os.environ),
        cwd=str(tmp_path),
        provider="claude",
    )
    result = run_turn(launch, should_stop=lambda: True)
    assert result.stopped is True
    assert result.exit_code != 0


def test_cli_dry_run_prints_invocation(capsys) -> None:
    rc = main(["start", "--provider", "claude", "--dry-run", "ship the thing"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "provider: claude" in out
    assert "argv:" in out
    assert "ship the thing" in out
