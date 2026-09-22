"""The Claude worker tool profile is code; the config file only records a person's deltas.

Until 2026-09-02 the only source of `--allowedTools` was `PPY_CLAUDE_ALLOWED_TOOLS`
in the supervisor's own environment. It was persisted nowhere, so every
`ppy supervisor serve` had to export it first and a dispatch after a restart
silently produced a worker with no shell. Then it was persisted as a verbatim copy,
and a widened profile (PR 19) never reached a machine that had set itself up
earlier. Now the profile lives in code and `config.toml` holds only what was added
to it or taken out of it.
"""

from __future__ import annotations

import time

import pytest

from papaya_agent_runtime import config, health, repos
from papaya_agent_runtime.cli import main
from papaya_agent_runtime.config import (
    MMConfig,
    WorkerCeiling,
    default_claude_allowed_tools,
    effective_claude_tools,
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

PROFILE_SOURCE = "built-in profile + config deltas"


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
    tools = effective_claude_tools(load_config())
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
    # The launcher is matched by a pattern: nothing in the profile names a machine.
    assert "Bash(*/bin/ppy:*)" in tools
    assert not any(t.startswith("Bash(/") for t in tools)
    # Deliberately absent: the classifier blocked these, and workers never open PRs.
    assert not any("gh:" in t or "rm:" in t or "export:" in t for t in tools)


def test_a_fresh_setup_stores_no_tools_and_no_defaults(ppy_home):
    """Goal 1: a new config carries nothing the code would arrive at by itself."""
    from papaya_agent_runtime.setup import wizard

    report = {
        "harnesses": [
            {"name": "claude", "available": True, "authenticated": True},
            {"name": "codex", "available": True, "authenticated": True},
        ],
        "requirements": [],
        "companions": [],
    }
    import unittest.mock as mock

    with (
        mock.patch.object(wizard, "discover", lambda: report),
        mock.patch.object(wizard, "usable_harnesses", lambda r: ["claude", "codex"]),
        mock.patch.object(wizard, "connection_harness", lambda: ""),
    ):
        wizard.run_setup(non_interactive=True)

    text = (ppy_home / "config.toml").read_text(encoding="utf-8")
    assert text.strip() == f"config_version = {config.CONFIG_VERSION}"
    assert "allowed_tools" not in text
    assert effective_allowed_tools() == (default_claude_allowed_tools(), PROFILE_SOURCE)


def test_a_new_code_profile_applies_on_the_next_load(configured, monkeypatch):
    """Goal 1: a release that changes the profile needs no config edit anywhere."""
    before, _ = effective_allowed_tools()
    monkeypatch.setattr(config, "CLAUDE_PROFILE", (*config.CLAUDE_PROFILE, "Bash(go:*)"))
    after, _ = effective_allowed_tools()
    assert after == [*before, "Bash(go:*)"]


# --------------------------------------------------------------------------- #
# Migration of a stored verbatim list
# --------------------------------------------------------------------------- #

#: Shane's `.ppy/config.toml` on 2026-09-16: the 20-entry 0.x profile, verbatim.
SHANES_STORED_LIST = [
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
    "Bash(/Users/shanewolf/workspace/papaya-agent-runtime/bin/ppy:*)",
    "Bash(./bin/ppy:*)",
    "Bash(ppy:*)",
    "Bash(ls:*)",
    "Bash(cat:*)",
    "Bash(mkdir:*)",
    "Bash(jq:*)",
    "Bash(pnpm:*)",
]


def _old_config(path, tools: list[str]) -> None:
    listed = ", ".join(f'"{t}"' for t in tools)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        'cost_posture = "lean"\n\n[manager]\nprovider = "claude"\nmodel = "opus"\n'
        'reasoning = "high"\n\n[worker]\nprovider = "claude"\nmax_model = "opus"\n'
        'max_reasoning = "medium"\ndefault_model = "opus"\ndefault_reasoning = "medium"\n'
        "max_concurrent = 2\n\n[health]\nquiet_minutes = 15\nplan_minutes = 10\n"
        f"max_stale_stacks = 4\n\n[claude]\nallowed_tools = [{listed}]\n",
        encoding="utf-8",
    )


def test_shanes_stored_profile_migrates_to_nothing(ppy_home):
    _old_config(ppy_home / "config.toml", SHANES_STORED_LIST)

    cfg = load_config()

    assert cfg.claude.allowed_tools is None
    assert cfg.claude.extra_tools == []
    assert cfg.claude.dropped_tools == []
    assert effective_claude_tools(cfg) == default_claude_allowed_tools()
    text = (ppy_home / "config.toml").read_text(encoding="utf-8")
    assert "/Users/shanewolf" not in text
    assert "allowed_tools" not in text
    assert "quiet_minutes" not in text  # a stored default is gone too
    assert f"config_version = {config.CONFIG_VERSION}" in text

    from papaya_agent_runtime import config_changes

    history = config_changes.history()
    assert [e["key"] for e in history] == ["claude.allowed_tools", "config_version"]
    assert "0.x profile of 2026-09-02" in history[0]["why"]
    # Once: the next load changes nothing and records nothing.
    load_config()
    assert len(config_changes.history()) == 2


# --------------------------------------------------------------------------- #
# Resolution: env beats config, config beats the built-in default
# --------------------------------------------------------------------------- #


def test_config_supplies_the_profile_with_no_env_var(configured):
    tools, source = effective_allowed_tools()
    assert tools == effective_claude_tools(load_config())
    assert source == PROFILE_SOURCE


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
    assert _allowed_tools_arg(argv) == ",".join(effective_claude_tools(load_config()))


def test_a_resumed_worker_gets_the_same_profile(configured):
    spec = _spec()
    spec.resume_session_id = "sess-1"
    argv = ClaudeAdapter().resume(spec)
    assert _allowed_tools_arg(argv) == ",".join(effective_claude_tools(load_config()))


def test_a_granted_bare_tool_name_and_a_literal_path_pattern_reach_the_launch(configured):
    """What a capability request grants is not always `Bash(<program>:*)`: a tool that
    is not the shell is allowed by its name, and a program run by path by that path."""
    spec = _spec()
    spec.granted_tools = ["WebSearch", "Bash(.venv/bin/python:*)", "mcp__docs__read"]
    allowed = _allowed_tools_arg(ClaudeAdapter().start(spec)).split(",")
    assert allowed[-3:] == ["WebSearch", "Bash(.venv/bin/python:*)", "mcp__docs__read"]
    assert "Bash(WebSearch:*)" not in allowed


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
    assert PROFILE_SOURCE in out


def test_health_flags_and_fails_when_there_are_no_tools(configured, monkeypatch, capsys):
    monkeypatch.setenv(ALLOWED_TOOLS_ENV, "")
    assert main(["health"]) == 1
    out = capsys.readouterr().out
    assert "NONE" in out
    assert "ppy config claude --reset" in out


def test_health_profile_is_machine_readable(configured):
    profile = health.claude_tool_profile()
    assert profile["ok"] is True
    assert profile["count"] == len(effective_claude_tools(load_config()))
    assert profile["source"] == PROFILE_SOURCE


# --------------------------------------------------------------------------- #
# ppy config claude
# --------------------------------------------------------------------------- #


def test_config_claude_edits_deltas_and_resets_them(configured):
    assert main(["config", "claude", "--allow", "Bash(go:*)", "--deny", "Bash(pnpm:*)"]) == 0
    cfg = load_config()
    assert cfg.claude.extra_tools == ["Bash(go:*)"]
    assert cfg.claude.dropped_tools == ["Bash(pnpm:*)"]
    assert "Bash(pnpm:*)" not in effective_claude_tools(cfg)

    assert main(["config", "claude", "--allow", "Bash(pnpm:*)"]) == 0
    assert load_config().claude.dropped_tools == []

    assert main(["config", "claude", "--reset"]) == 0
    cfg = load_config()
    assert (cfg.claude.extra_tools, cfg.claude.dropped_tools) == ([], [])
    assert effective_claude_tools(cfg) == default_claude_allowed_tools()


def test_config_claude_show_marks_where_each_tool_comes_from(configured, capsys):
    assert main(["config", "claude", "--allow", "Bash(go:*)", "--deny", "Bash(jq:*)"]) == 0
    capsys.readouterr()

    assert main(["config", "claude", "--show"]) == 0
    rows = {
        line.split()[1]: line.split()[0]
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("  ") and not line.strip().startswith("locked")
    }
    assert rows["Bash(git:*)"] == "profile"
    assert rows["Bash(go:*)"] == "extra"
    assert rows["Bash(jq:*)"] == "dropped"


#: Added 2026-09-16: every JavaScript worker was refused `node --test`, and one could
#: not `cp` its evidence into place, because the profile came from a Python-only manager.
GATE_AND_FILE_TOOLS = (
    "Bash(node:*)",
    "Bash(npm:*)",
    "Bash(npx:*)",
    "Bash(corepack:*)",
    "Bash(cp:*)",
    "Bash(mv:*)",
    "Bash(tee:*)",
    "Bash(touch:*)",
    "Bash(head:*)",
    "Bash(tail:*)",
    "Bash(wc:*)",
    "Bash(sed:*)",
    "Bash(find:*)",
    "Bash(python3:*)",
    "Bash(sqlite3:*)",
)


def test_reset_restores_a_profile_that_can_run_javascript_gates_and_move_files(configured):
    missing = [t for t in GATE_AND_FILE_TOOLS if t not in default_claude_allowed_tools()]
    assert missing == []

    assert main(["config", "claude", "--deny", "Bash(node:*)", "--deny", "Bash(cp:*)"]) == 0
    assert main(["config", "claude", "--reset"]) == 0
    effective = effective_claude_tools(load_config())
    assert [t for t in GATE_AND_FILE_TOOLS if t not in effective] == []
