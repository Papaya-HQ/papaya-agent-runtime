"""A worker asks for what it lacks; the runtime grants it by policy or a person decides.

2026-09-17: `xcodegen` was denied four times and a generated Xcode project was edited by
hand; a browser capture was found refused after the code was done; a `psql` denial sat
as a readiness gap nobody decided. Every install allows different things, so the
decision is this install's policy first and a person's second, never the worker's guess.
"""

from __future__ import annotations

import json

import pytest

from papaya_agent_runtime import capability_requests as cr
from papaya_agent_runtime import config_changes, readiness, tool_learning
from papaya_agent_runtime.cli import main
from papaya_agent_runtime.config import ConfigError, MMConfig, load_config, save_config
from papaya_agent_runtime.providers.base import TaskSpec
from papaya_agent_runtime.providers.claude import ALLOWED_TOOLS_ENV, ClaudeAdapter
from papaya_agent_runtime.state import init_db, store


@pytest.fixture
def home(ppy_home, monkeypatch):
    monkeypatch.delenv(ALLOWED_TOOLS_ENV, raising=False)
    save_config(MMConfig())
    steered: list[tuple[int, str]] = []
    monkeypatch.setattr(tool_learning, "steer_worker", lambda t, m: steered.append((t, m)))
    return steered


def _task(status="in_progress") -> int:
    conn = init_db()
    try:
        task_id = store.add_task(conn, run_id=store.create_run(conn, "ios"), title="channels")
        store.set_task_status(conn, task_id, status)
        return task_id
    finally:
        conn.close()


def _launch_tools(task_id: int) -> list[str]:
    conn = init_db()
    try:
        granted = cr.granted_patterns(conn, task_id)
    finally:
        conn.close()
    spec = TaskSpec(
        task_id=task_id,
        title="t",
        instructions="",
        worktree_path="/w",
        base_sha="abc",
        provider="claude",
        granted_tools=granted,
    )
    argv = ClaudeAdapter().start(spec)
    return argv[argv.index("--allowedTools") + 1].split(",")


# ── the state table ─────────────────────────────────────────────────────────


def test_a_program_policy_does_not_cover_waits_on_a_person_and_is_one_request(home) -> None:
    task_id = _task()

    first = cr.request(task_id, "xcodegen", why="regenerate the Xcode project")
    again = cr.request(task_id, "Bash(xcodegen:*)", why="asked twice")

    assert first.state == cr.PENDING
    assert again.id == first.id
    assert home == []  # a declared, pending request is answered by the command itself
    conn = init_db()
    assert [r.id for r in cr.pending(conn)] == [first.id]


def test_the_safe_family_and_the_install_policy_grant_without_a_person(home) -> None:
    task_id = _task()
    cfg = load_config()
    cfg.capabilities.auto_grant = ["xcodegen"]
    save_config(cfg)

    granted = cr.request(task_id, "xcodegen")
    family = cr.request(task_id, "jq")

    assert (granted.state, family.state) == (cr.AUTO_GRANTED, cr.AUTO_GRANTED)
    assert "Bash(xcodegen:*)" in load_config().claude.extra_tools
    # A grant reaches a running worker only through a relaunch, so it is steered.
    assert any("granted" in message for _task_id, message in home)


def test_the_floor_is_refused_and_no_config_can_lower_it(home) -> None:
    task_id = _task()

    refused = cr.request(task_id, "curl")

    assert refused.state == cr.REFUSED and refused.reason
    cfg = load_config()
    cfg.capabilities.auto_grant = ["sudo"]
    save_config(cfg)
    assert cr.decide("sudo") == cr.REFUSED
    assert main(["config", "capabilities", "--auto-grant", "ssh"]) == 1


def test_an_install_never_list_refuses_what_a_person_would_otherwise_be_asked(home) -> None:
    assert main(["config", "capabilities", "--never", "psql"]) == 0
    assert cr.request(_task(), "psql").state == cr.REFUSED
    assert config_changes.history()[0]["key"] == "capabilities"


@pytest.mark.parametrize("bad", ["/usr/bin/xcodegen", "Bash(*)", "make && rm -rf /", "a b"])
def test_only_one_named_program_can_be_asked_for(home, bad) -> None:
    with pytest.raises(cr.CapabilityError):
        cr.request(_task(), bad)


def test_config_holds_program_names_only(ppy_home) -> None:
    cfg = MMConfig()
    cfg.capabilities.auto_grant = ["Bash(*)"]
    with pytest.raises(ConfigError):
        cfg.validate()


# ── a person's answer ───────────────────────────────────────────────────────


def test_approving_grants_the_task_alone_and_its_next_launch_carries_it(home) -> None:
    task_id, other = _task(), _task()
    request = cr.request(task_id, "xcodegen", why="regenerate the project")

    decided = cr.decide_request(request.id, approve=True)

    assert decided.state == cr.GRANTED and decided.scope == "task"
    assert "Bash(xcodegen:*)" in _launch_tools(task_id)
    assert "Bash(xcodegen:*)" not in _launch_tools(other)
    assert "Bash(xcodegen:*)" not in load_config().claude.extra_tools
    assert any(t == task_id and "granted" in m for t, m in home)
    conn = init_db()
    row = conn.execute("SELECT answer, scope FROM decisions").fetchone()
    assert (row["answer"], row["scope"]) == ("granted", "task")


def test_approving_always_grants_every_worker_on_this_machine(home) -> None:
    task_id = _task()
    request = cr.request(task_id, "xcodegen")

    decided = cr.decide_request(request.id, approve=True, always=True)

    assert decided.scope == "install"
    assert "Bash(xcodegen:*)" in load_config().claude.extra_tools
    assert "Bash(xcodegen:*)" in _launch_tools(_task())
    assert config_changes.history()[0]["evidence"]["request_id"] == request.id


def test_denying_needs_a_reason_and_tells_the_worker_it(home) -> None:
    task_id = _task()
    request = cr.request(task_id, "psql")

    with pytest.raises(cr.CapabilityError):
        cr.decide_request(request.id, approve=False)
    decided = cr.decide_request(request.id, approve=False, reason="use the migrations instead")

    assert decided.state == cr.DENIED
    assert any("use the migrations instead" in m for _t, m in home)
    assert _launch_tools(task_id).count("Bash(psql:*)") == 0


def test_a_resolved_request_or_an_ended_task_cannot_be_answered(home) -> None:
    task_id = _task()
    request = cr.request(task_id, "xcodegen")
    cr.decide_request(request.id, approve=True)
    with pytest.raises(cr.CapabilityError, match="already granted"):
        cr.decide_request(request.id, approve=False, reason="changed my mind")

    closed = _task()
    late = cr.request(closed, "psql")
    conn = init_db()
    store.set_task_status(conn, closed, "closed")
    with pytest.raises(cr.CapabilityError, match="closed"):
        cr.decide_request(late.id, approve=True)


# ── from a denial, and in front of a person ─────────────────────────────────


def test_a_denied_plain_command_becomes_a_request_a_person_is_asked_about(home) -> None:
    task_id = _task()
    denial = {
        "tool_name": "Bash",
        "tool_use_id": "toolu_1",
        "tool_input": {"command": "xcodegen generate"},
    }

    tool_learning.learn([denial], task_id=task_id, run_id=None, worktree="/w")

    conn = init_db()
    [request] = cr.pending(conn)
    assert (request.program, request.source, request.command) == (
        "xcodegen",
        cr.DENIAL,
        "xcodegen generate",
    )
    assert any("waiting on a person" in m for _t, m in home)
    [problem] = [p for p in readiness.check().problems if p.code == cr.PROBLEM_CODE]
    assert problem.owner == readiness.USER and not problem.blocking
    assert f"ppy capability approve {request.id}" in "\n".join(problem.steps)
    assert problem.scope == f"capability:{request.id}"


def test_the_cli_declares_lists_and_answers(home, capsys) -> None:
    task_id = _task()

    assert main(["need", str(task_id), "--capability", "xcodegen", "--why", "regen"]) == 0
    assert "waiting on a person" in capsys.readouterr().out
    assert main(["capability", "list", "--json"]) == 0
    [listed] = json.loads(capsys.readouterr().out)
    assert main(["capability", "approve", str(listed["id"])]) == 0
    assert "task" in capsys.readouterr().out
    assert main(["capability", "list"]) == 0
    assert "no capability requests waiting on a person" in capsys.readouterr().out


def test_the_worker_rules_say_to_ask_in_the_plan_phase() -> None:
    from papaya_agent_runtime.providers.command_rules import command_rules

    assert "ppy need <task id>" in command_rules("claude", "ppy/task-1-x")
