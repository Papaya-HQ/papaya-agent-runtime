"""The Claude worker tool profile is configuration, not an environment accident.

Until 2026-09-02 the only source of `--allowedTools` was `PPY_CLAUDE_ALLOWED_TOOLS`
in the supervisor's own environment. It was persisted nowhere, so every
`ppy supervisor serve` had to export it first and a dispatch after a restart
silently produced a worker with no shell.
"""

from __future__ import annotations

import time

import pytest

from papaya_agent_runtime import health, repos
from papaya_agent_runtime.cli import main
from papaya_agent_runtime.config import (
    ConfigError,
    MMConfig,
    WorkerCeiling,
    default_claude_allowed_tools,
    load_config,
    save_config,
)
from papaya_agent_runtime.providers.base import TaskSpec
from papaya_agent_runtime.providers.claude import (
    ALLOWED_TOOLS_ENV,
    ClaudeAdapter,
    effective_allowed_tools,
)
from papaya_agent_runtime.supervisor.client import SupervisorClient
from papaya_agent_runtime.supervisor.core import SupervisorError
from papaya_agent_runtime.supervisor.server import SupervisorServer


@pytest.fixture(autouse=True)
def _no_env_profile(monkeypatch):
    """Start every case from "the env var is not set", whatever the dev machine does."""
    monkeypatch.delenv(ALLOWED_TOOLS_ENV, raising=False)


@pytest.fixture
def configured(ppy_home):
    cfg = MMConfig()
    save_config(cfg)
    return cfg


def _spec():
    return TaskSpec(
        task_id=1,
        title="t",
        instructions="do the thing",
        worktree_path="/tmp/wt",
        base_sha="abc",
        provider="claude",
    )


def _allowed_tools_arg(argv: list[str]) -> str | None:
    return argv[argv.index("--allowedTools") + 1] if "--allowedTools" in argv else None


# --------------------------------------------------------------------------- #
# The default profile
# --------------------------------------------------------------------------- #


def test_default_profile_is_the_documented_one(configured):
    tools = load_config().claude.allowed_tools
    for expected in (
        "Read",
        "Edit",
        "Write",
        "Glob",
        "Grep",
        "Bash(cd:*)",
        "Bash(git:*)",
        "Bash(uv:*)",
        "Bash(make:*)",
        "Bash(pytest:*)",
        "Bash(python:*)",
        "Bash(ruff:*)",
        "Bash(./bin/ppy:*)",
        "Bash(ppy:*)",
        "Bash(ls:*)",
        "Bash(cat:*)",
        "Bash(mkdir:*)",
        "Bash(jq:*)",
        "Bash(pnpm:*)",
    ):
        assert expected in tools
    assert any(t.startswith("Bash(/") and t.endswith("bin/ppy:*)") for t in tools), (
        "the profile must carry the absolute path to this instance's bin/ppy"
    )
    # Deliberately absent: the classifier blocked these, and workers never open PRs.
    assert not any("gh:" in t or "rm:" in t or "export:" in t for t in tools)


def test_the_profile_round_trips_through_the_config_file(configured):
    cfg = load_config()
    cfg.claude.allowed_tools = ["Read", "Bash(git:*)"]
    save_config(cfg)
    assert load_config().claude.allowed_tools == ["Read", "Bash(git:*)"]


def test_a_bad_pattern_is_refused(configured):
    cfg = load_config()
    cfg.claude.allowed_tools = ["Read", "   "]
    with pytest.raises(ConfigError) as exc:
        save_config(cfg)
    assert "claude.allowed_tools" in str(exc.value)


# --------------------------------------------------------------------------- #
# Resolution: env beats config, config beats the built-in default
# --------------------------------------------------------------------------- #


def test_config_supplies_the_profile_with_no_env_var(configured):
    tools, source = effective_allowed_tools()
    assert tools == load_config().claude.allowed_tools
    assert source == "config claude.allowed_tools"


def test_the_env_var_overrides_the_config(configured, monkeypatch):
    monkeypatch.setenv(ALLOWED_TOOLS_ENV, "Read, Bash(git:*) ,Grep")
    tools, source = effective_allowed_tools()
    assert tools == ["Read", "Bash(git:*)", "Grep"]
    assert source == ALLOWED_TOOLS_ENV


def test_without_a_config_file_the_documented_default_still_applies(ppy_home):
    tools, source = effective_allowed_tools()
    assert tools == default_claude_allowed_tools()
    assert source == "built-in default profile"


def test_an_emptied_env_var_means_no_tools(configured, monkeypatch):
    monkeypatch.setenv(ALLOWED_TOOLS_ENV, "")
    tools, _source = effective_allowed_tools()
    assert tools == []


# --------------------------------------------------------------------------- #
# The adapter launches with the effective profile
# --------------------------------------------------------------------------- #


def test_the_worker_is_launched_with_the_configured_profile(configured):
    argv = ClaudeAdapter().start(_spec())
    assert _allowed_tools_arg(argv) == ",".join(load_config().claude.allowed_tools)


def test_a_resumed_worker_gets_the_same_profile(configured):
    spec = _spec()
    spec.resume_session_id = "sess-1"
    argv = ClaudeAdapter().resume(spec)
    assert _allowed_tools_arg(argv) == ",".join(load_config().claude.allowed_tools)


def test_an_empty_profile_passes_no_allowlist_flag(configured, monkeypatch):
    monkeypatch.setenv(ALLOWED_TOOLS_ENV, "")
    assert _allowed_tools_arg(ClaudeAdapter().start(_spec())) is None


# --------------------------------------------------------------------------- #
# Dispatch refuses a shell-less Claude worker
# --------------------------------------------------------------------------- #


@pytest.fixture
def server(ppy_home):
    srv = SupervisorServer()
    srv.start_background()
    client = SupervisorClient(srv.socket_path)
    for _ in range(50):
        try:
            if client.ping().get("ok"):
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.05)
    yield srv, client
    srv.stop()


def test_dispatch_refuses_a_claude_worker_with_no_tools(server, source_repo, monkeypatch):
    srv, client = server
    save_config(MMConfig(worker=WorkerCeiling("claude", "sonnet", "medium")))
    added = repos.add_repo(source_repo)
    monkeypatch.setenv(ALLOWED_TOOLS_ENV, "")

    with pytest.raises(SupervisorError) as exc:
        srv.supervisor.dispatch_task(repo=added.name, title="no shell", provider="claude")

    message = str(exc.value)
    assert "no tools at all" in message
    assert "ppy config claude --reset" in message
    # Refused before any state existed: no task row, no lease.
    from papaya_agent_runtime.state import init_db

    conn = init_db()
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0


def test_a_codex_dispatch_is_unaffected_by_the_claude_profile(server, source_repo, monkeypatch):
    srv, client = server
    save_config(MMConfig())
    repos.add_repo(source_repo)
    monkeypatch.setenv(ALLOWED_TOOLS_ENV, "")
    # Codex has its own sandbox; the Claude allowlist says nothing about it. The
    # dispatch gets far enough to fail on something else, never on tools.
    try:
        srv.supervisor.dispatch_task(repo="source", title="codex", provider="codex")
    except SupervisorError as exc:
        assert "no tools at all" not in str(exc)


# --------------------------------------------------------------------------- #
# ppy health reports the effective profile
# --------------------------------------------------------------------------- #


def test_health_reports_the_effective_profile(configured, capsys):
    assert main(["health"]) == 0
    out = capsys.readouterr().out
    assert "claude worker tools:" in out
    assert "config claude.allowed_tools" in out


def test_health_flags_and_fails_when_there_are_no_tools(configured, monkeypatch, capsys):
    monkeypatch.setenv(ALLOWED_TOOLS_ENV, "")
    assert main(["health"]) == 1
    out = capsys.readouterr().out
    assert "NONE" in out
    assert "ppy config claude --reset" in out


def test_health_profile_is_machine_readable(configured):
    profile = health.claude_tool_profile()
    assert profile["ok"] is True
    assert profile["count"] == len(load_config().claude.allowed_tools)
    assert profile["source"] == "config claude.allowed_tools"


# --------------------------------------------------------------------------- #
# ppy config claude
# --------------------------------------------------------------------------- #


def test_config_claude_sets_and_resets_the_profile(configured, capsys):
    assert main(["config", "claude", "--allowed-tools", "Read,Bash(uv:*)"]) == 0
    assert load_config().claude.allowed_tools == ["Read", "Bash(uv:*)"]
    assert "2 patterns" in capsys.readouterr().out

    assert main(["config", "claude", "--reset"]) == 0
    assert load_config().claude.allowed_tools == default_claude_allowed_tools()
