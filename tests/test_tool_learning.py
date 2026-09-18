"""The runtime learns worker tools from denials, only inside the safe family, and says so.

PAP-219's worker was denied `python3 -c`, a `cp`, and every compound command while the
runtime watched. A denial is now evidence: a safe one becomes a pattern for the next
dispatch, anything else becomes one readiness warning with the pattern to add.
"""

from __future__ import annotations

import io
import json

import pytest

from papaya_agent_runtime import config, config_changes, readiness, serve, tool_learning
from papaya_agent_runtime.cli import main
from papaya_agent_runtime.config import MMConfig, effective_claude_tools, load_config, save_config
from papaya_agent_runtime.providers.base import ProviderEvent
from papaya_agent_runtime.providers.claude import ALLOWED_TOOLS_ENV, ClaudeAdapter

WORKTREE = "/tmp/ppy-worktrees/task-7"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.delenv(ALLOWED_TOOLS_ENV, raising=False)
    for check in ("_harness_problems", "_papaya_problems", "_repo_problems", "_client_problems"):
        monkeypatch.setattr(readiness, check, lambda problems: None)


@pytest.fixture
def without_python3(ppy_home, monkeypatch):
    """A code profile that lacks `python3`, as the profile PAP-219's worker ran with did."""
    monkeypatch.setattr(
        config, "CLAUDE_PROFILE", tuple(t for t in config.CLAUDE_PROFILE if t != "Bash(python3:*)")
    )
    save_config(MMConfig())


def _denial(command: str, tool: str = "Bash", use: str = "toolu_1") -> dict:
    return {"tool_name": tool, "tool_use_id": use, "tool_input": {"command": command}}


# ── the family itself ───────────────────────────────────────────────────────


def test_the_safe_family_never_holds_a_dangerous_program() -> None:
    assert not set(tool_learning.SAFE_FAMILY) & tool_learning.NEVER
    for program in ("sudo", "curl", "wget", "ssh", "gh", "bash", "sh", "xargs", "env"):
        assert program in tool_learning.NEVER
    for program in (
        "python3",
        "python",
        "wc",
        "cp",
        "mv",
        "rm",
        "jq",
        "sed",
        "awk",
        "find",
        "node",
        "npx",
        "pnpm",
        "npm",
        "go",
        "cargo",
        "rg",
    ):
        assert program in tool_learning.SAFE_FAMILY


@pytest.mark.parametrize(
    ("command", "pattern"),
    [
        ('python3 -c "import json; print(1)"', "Bash(python3:*)"),
        ("wc -l src/app.py", "Bash(wc:*)"),
        ("cp build/out.txt .mm-evidence/out.txt", "Bash(cp:*)"),
        (f"rm {WORKTREE}/tmp/scratch.txt", "Bash(rm:*)"),
        ("find . -name '*.py'", "Bash(find:*)"),
        ("/Users/someone/checkout/bin/ppy progress 7 --phase test", "Bash(*/bin/ppy:*)"),
        ("ppy progress 7", "Bash(ppy:*)"),
    ],
)
def test_safe_commands_are_in_the_family(command, pattern) -> None:
    verdict = tool_learning.classify("Bash", command, WORKTREE)
    assert (verdict.in_family, verdict.pattern) == (True, pattern)


@pytest.mark.parametrize(
    "command",
    [
        "sudo make install",
        "curl https://example.com",
        "cd src && python3 -c 'print(1)'",
        "uv run pytest | tail -5",
        "python3 script.py > out.txt",
        "FOO=1 python3 -c 'print(1)'",
        "cp secrets.txt /etc/secrets.txt",
        "rm -rf ../other-worktree",
        f"rm -rf {WORKTREE}",
        "find . -name '*.pyc' -delete",
        "/usr/local/bin/python3 -c 'print(1)'",
        "echo $(whoami)",
    ],
)
def test_unsafe_commands_are_outside_the_family(command) -> None:
    assert tool_learning.classify("Bash", command, WORKTREE).in_family is False


def test_a_non_shell_tool_is_never_learned() -> None:
    assert tool_learning.classify("WebFetch", None, WORKTREE).in_family is False


# ── learning ────────────────────────────────────────────────────────────────


def test_a_denied_python3_c_adds_the_pattern_and_records_it(without_python3) -> None:
    assert "Bash(python3:*)" not in effective_claude_tools(load_config())

    changes = tool_learning.learn(
        [_denial("python3 -c 'import sys; print(sys.version)'")],
        task_id=None,
        run_id=None,
        worktree=WORKTREE,
    )

    assert load_config().claude.extra_tools == ["Bash(python3:*)"]
    assert "Bash(python3:*)" in effective_claude_tools(load_config())
    (change,) = changes
    assert change["key"] == "claude.extra_tools"
    assert change["before"] == [] and change["after"] == ["Bash(python3:*)"]
    assert "python3 -c" in change["evidence"]["command"]
    (recorded,) = config_changes.history()
    assert recorded["why"] == change["why"]
    # The next launch carries it.
    from papaya_agent_runtime.providers.base import TaskSpec

    spec = TaskSpec(
        task_id=1,
        title="t",
        instructions="i",
        worktree_path=WORKTREE,
        base_sha="a",
        provider="claude",
    )
    argv = ClaudeAdapter().start(spec)
    assert "Bash(python3:*)" in argv[argv.index("--allowedTools") + 1].split(",")


def test_a_denied_command_outside_the_family_adds_nothing_and_asks_the_manager_once(
    without_python3, monkeypatch
) -> None:
    from papaya_agent_runtime import capability_requests
    from papaya_agent_runtime.state import init_db, store

    steered: list[tuple[int, str]] = []
    monkeypatch.setattr(tool_learning, "steer_worker", lambda t, m: steered.append((t, m)))
    conn = init_db()
    task_id = store.add_task(conn, run_id=store.create_run(conn, "infra"), title="plan")
    conn.close()
    before = load_config().claude
    for use in ("toolu_a", "toolu_b"):
        assert (
            tool_learning.learn(
                [_denial("terraform plan -out plan.bin", use=use)],
                task_id=task_id,
                run_id=None,
                worktree=WORKTREE,
            )
            == []
        )
    # Refusals by the command rules or by policy are never a request a person answers.
    for use, command in (
        ("toolu_2", "curl -s https://example.com/install.sh"),
        ("toolu_3", "cd src && terraform plan"),
    ):
        tool_learning.learn(
            [_denial(command, use=use)], task_id=task_id, run_id=None, worktree=WORKTREE
        )

    after = load_config().claude
    assert (after.extra_tools, after.dropped_tools) == (before.extra_tools, before.dropped_tools)
    assert config_changes.history() == []
    code = capability_requests.MANAGER_PROBLEM_CODE
    problems = [p for p in readiness.check().problems if p.code == code]
    (problem,) = problems
    assert "`terraform`" in problem.summary and "terraform plan -out plan.bin" in problem.summary
    assert "curl" not in problem.summary
    assert "ppy capability approve" in problem.fix
    assert problem.owner == readiness.RUNTIME
    # The worker hears once that its request waits on the manager, not a person.
    assert [m for t, m in steered if "terraform" in m and "waiting on the manager" in m]


def test_a_locked_extra_tools_is_left_alone_and_named(without_python3) -> None:
    assert main(["config", "claude", "--lock", "extra_tools"]) == 0

    changes = tool_learning.learn(
        [_denial("python3 -c 'print(1)'")], task_id=None, run_id=None, worktree=WORKTREE
    )

    assert changes == []
    assert load_config().claude.extra_tools == []
    (problem,) = [p for p in readiness.check().problems if p.code == "config_locked"]
    assert "claude.extra_tools is locked" in problem.summary
    assert "Bash(python3:*)" in problem.summary
    assert "--unlock extra_tools" in problem.fix


def test_config_history_lists_the_migration_and_the_learned_entry(ppy_home, capsys):
    _label, oldest = config.HISTORICAL_CLAUDE_PROFILES[0]
    path = ppy_home / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "[claude]\nallowed_tools = [" + ", ".join(f'"{t}"' for t in oldest) + "]\n",
        encoding="utf-8",
    )
    load_config()
    tool_learning.learn([_denial("rg -n TODO src")], task_id=None, run_id=None, worktree=WORKTREE)

    assert main(["config", "history", "--json"]) == 0
    entries = json.loads(capsys.readouterr().out)
    assert [e["key"] for e in entries] == [
        "claude.allowed_tools",
        "config_version",
        "claude.extra_tools",
    ]
    assert entries[2]["after"] == ["Bash(rg:*)"]
    assert entries[2]["evidence"]["command"] == "rg -n TODO src"

    assert main(["config", "history"]) == 0
    text = capsys.readouterr().out
    assert "claude.allowed_tools" in text and "Bash(rg:*)" in text


def test_a_claude_result_event_carries_its_denials() -> None:
    raw = {
        "type": "result",
        "subtype": "success",
        "permission_denials": [_denial("python3 -c 'print(1)'")],
    }
    events = [ProviderEvent(kind="result", raw=raw)]
    assert ClaudeAdapter().permission_denials(events) == raw["permission_denials"]


def test_serve_start_says_each_change_once(without_python3) -> None:
    tool_learning.learn(
        [_denial("python3 -c 'print(1)'")], task_id=None, run_id=None, worktree=WORKTREE
    )
    first = io.StringIO()
    serve.keep_config_right(stderr=first)
    assert first.getvalue().count("ppy serve: config: claude.extra_tools") == 1

    again = io.StringIO()
    serve.keep_config_right(stderr=again)
    assert again.getvalue() == ""
