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

    first = cr.request(task_id, "terraform", why="plan the staging stack")
    again = cr.request(task_id, "Bash(terraform:*)", why="asked twice")

    assert first.state == cr.PENDING
    assert again.id == first.id
    assert home == []  # a declared, pending request is answered by the command itself
    conn = init_db()
    assert [r.id for r in cr.pending(conn)] == [first.id]


def test_the_safe_family_and_the_install_policy_grant_without_a_person(home) -> None:
    task_id = _task()
    cfg = load_config()
    cfg.capabilities.auto_grant = ["terraform"]
    save_config(cfg)

    granted = cr.request(task_id, "terraform")
    family = cr.request(task_id, "jq")

    assert (granted.state, family.state) == (cr.AUTO_GRANTED, cr.AUTO_GRANTED)
    assert "Bash(terraform:*)" in load_config().claude.extra_tools
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
    request = cr.request(task_id, "terraform", why="plan the stack")

    decided = cr.decide_request(request.id, approve=True)

    assert decided.state == cr.GRANTED and decided.scope == "task"
    assert "Bash(terraform:*)" in _launch_tools(task_id)
    assert "Bash(terraform:*)" not in _launch_tools(other)
    assert "Bash(terraform:*)" not in load_config().claude.extra_tools
    assert any(t == task_id and "granted" in m for t, m in home)
    conn = init_db()
    row = conn.execute("SELECT answer, scope FROM decisions").fetchone()
    assert (row["answer"], row["scope"]) == ("granted", "task")


def test_approving_always_grants_every_worker_on_this_machine(home) -> None:
    task_id = _task()
    request = cr.request(task_id, "terraform")

    decided = cr.decide_request(request.id, approve=True, always=True)

    assert decided.scope == "install"
    assert "Bash(terraform:*)" in load_config().claude.extra_tools
    assert "Bash(terraform:*)" in _launch_tools(_task())
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
    request = cr.request(task_id, "terraform")
    cr.decide_request(request.id, approve=True)
    with pytest.raises(cr.CapabilityError, match="already granted"):
        cr.decide_request(request.id, approve=False, reason="changed my mind")

    closed = _task()
    late = cr.request(closed, "psql")
    conn = init_db()
    store.set_task_status(conn, closed, "closed")
    with pytest.raises(cr.CapabilityError, match="moot"):
        cr.decide_request(late.id, approve=True)


def test_a_request_on_a_task_that_ended_is_moot_and_asks_nobody(home) -> None:
    task_id = _task()
    request = cr.request(task_id, "terraform")
    conn = init_db()
    store.set_task_status(conn, task_id, "delivered")
    assert cr.pending(conn) == []
    assert cr.get(conn, request.id).state == cr.MOOT
    assert not [p for p in readiness.check().problems if p.code == cr.PROBLEM_CODE]


def test_the_tools_workers_asked_a_person_for_are_granted_by_the_runtime(home) -> None:
    task_id = _task()
    for program in ("chrome-devtools-axi", "nvm", "shasum", "ps", "xcodegen", "xcodebuild"):
        assert cr.request(task_id, program).state == cr.AUTO_GRANTED, program


def test_the_gh_wrapper_and_kill_are_refused_like_gh(home) -> None:
    task_id = _task()
    wrapped = cr.request(task_id, "gh-axi")
    assert wrapped.state == cr.REFUSED
    assert "pull requests is the runtime's" in (wrapped.reason or "")
    assert cr.request(task_id, "kill").state == cr.REFUSED


# ── from a denial, and in front of a person ─────────────────────────────────


def test_a_denied_plain_command_becomes_a_request_the_manager_decides(home) -> None:
    task_id = _task()
    denial = {
        "tool_name": "Bash",
        "tool_use_id": "toolu_1",
        "tool_input": {"command": "terraform plan"},
    }

    tool_learning.learn([denial], task_id=task_id, run_id=None, worktree="/w")

    conn = init_db()
    [request] = cr.pending(conn)
    assert (request.program, request.source, request.command) == (
        "terraform",
        cr.DENIAL,
        "terraform plan",
    )
    assert any("waiting on the manager" in m for _t, m in home)
    problems = readiness.check().problems
    assert not [p for p in problems if p.code == cr.PROBLEM_CODE]
    [mine] = [p for p in problems if p.code == cr.MANAGER_PROBLEM_CODE]
    assert mine.owner == readiness.RUNTIME and not mine.blocking
    assert f"ppy capability approve {request.id}" in mine.fix
    assert mine.scope == f"capability:{request.id}"


def test_only_an_escalated_request_is_a_person_s_and_they_hear_why(home) -> None:
    task_id = _task()
    request = cr.request(task_id, "terraform", why="plan the staging stack")
    with pytest.raises(cr.CapabilityError, match="only a person can decide"):
        cr.escalate(request.id, why="  ")
    raised = cr.escalate(request.id, why="it needs the cloud account's credentials")
    assert raised.state == cr.ESCALATED
    conn = init_db()
    assert cr.pending(conn) == [] and [r.id for r in cr.escalated(conn)] == [request.id]
    [problem] = [p for p in readiness.check().problems if p.code == cr.PROBLEM_CODE]
    assert problem.owner == readiness.USER
    assert "cloud account's credentials" in "\n".join(problem.steps)
    # A person's answer still decides it.
    assert cr.decide_request(request.id, approve=True).state == cr.GRANTED
    with pytest.raises(cr.CapabilityError, match="already granted"):
        cr.escalate(request.id, why="again")


def test_the_cli_declares_lists_and_answers(home, capsys) -> None:
    task_id = _task()

    assert main(["need", str(task_id), "--capability", "terraform", "--why", "regen"]) == 0
    assert "waiting on the manager" in capsys.readouterr().out
    assert main(["capability", "list", "--json"]) == 0
    [listed] = json.loads(capsys.readouterr().out)
    assert main(["capability", "escalate", str(listed["id"]), "--why", "cloud credentials"]) == 0
    assert "escalated" in capsys.readouterr().out
    assert main(["capability", "approve", str(listed["id"])]) == 0
    assert "task" in capsys.readouterr().out
    assert main(["capability", "list"]) == 0
    assert "no capability requests waiting on a decision" in capsys.readouterr().out


def test_the_worker_rules_say_to_ask_in_the_plan_phase() -> None:
    from papaya_agent_runtime.providers.command_rules import command_rules

    assert "ppy need <task id>" in command_rules("claude", "ppy/task-1-x")


# ── every denial enters the loop, whatever the tool and whatever the stack ──
#
# 2026-09-22 (issues #139, #140, #142): a denied `WebFetch` could only ever become a
# GitHub issue, `.venv/bin/python` became a request for `python` whose grant never
# matched the refused command, and `export PATH=…` became a request for a program
# called `export`.


def _deny(task_id: int, command: str | None, *, tool: str = "Bash", worktree=None, use="t1"):
    denial = {"tool_name": tool, "tool_use_id": use, "tool_input": {}}
    if command is not None:
        denial["tool_input"] = {"command": command}
    tool_learning.learn([denial], task_id=task_id, run_id=None, worktree=worktree)
    conn = init_db()
    try:
        return cr.all_requests(conn, task_id=task_id)
    finally:
        conn.close()


@pytest.fixture
def worktree(tmp_path):
    """A worktree with its own interpreter in `.venv`, as a provisioned one has."""
    root = tmp_path / "wt"
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
    return root


def _policy(auto_grant=(), never=()) -> None:
    cfg = load_config()
    cfg.capabilities.auto_grant = list(auto_grant)
    cfg.capabilities.never = list(never)
    save_config(cfg)


# (a) a tool that is not the shell


def test_a_denied_web_search_is_a_request_for_the_tool_the_manager_decides(home) -> None:
    task_id = _task()

    [request] = _deny(task_id, None, tool="WebSearch")

    assert (request.program, request.pattern, request.state) == (
        "WebSearch",
        "WebSearch",
        cr.PENDING,
    )
    assert any("`WebSearch`" in m and "waiting on the manager" in m for _t, m in home)


def test_a_web_search_the_install_never_grants_is_refused_with_the_rule(home) -> None:
    _policy(never=["WebSearch"])

    [request] = _deny(_task(), None, tool="WebSearch")

    assert request.state == cr.REFUSED
    assert request.reason == tool_learning.policy_rule("WebSearch")


def test_a_web_search_the_install_grants_is_in_the_next_launch_by_its_own_name(home) -> None:
    _policy(auto_grant=["WebSearch"])
    task_id = _task()

    [request] = _deny(task_id, None, tool="WebSearch")

    assert request.state == cr.AUTO_GRANTED
    tools = _launch_tools(task_id)
    assert "WebSearch" in tools and "Bash(WebSearch:*)" not in tools
    assert "WebSearch" in load_config().claude.extra_tools


def test_an_mcp_tool_name_is_a_capability_too(home) -> None:
    _policy(never=["mcp__docs__delete"])
    [request] = _deny(_task(), None, tool="mcp__docs__delete")
    assert (request.pattern, request.state) == ("mcp__docs__delete", cr.REFUSED)


@pytest.mark.parametrize("bad", ["Bash", "Bash(*)", "Web Fetch", "Web(Fetch)", "a:b"])
def test_the_shell_itself_or_a_malformed_tool_is_never_a_capability(home, bad) -> None:
    with pytest.raises(cr.CapabilityError):
        cr.request(_task(), bad)
    verdict = tool_learning.classify(bad, None, "/w")
    assert verdict.pattern in ("", bad) and not verdict.pattern.startswith("Bash(")
    if verdict.pattern:
        assert cr.is_tool(bad)


def test_a_tool_the_worker_already_has_refused_is_where_it_pointed(home) -> None:
    verdict = tool_learning.classify("Read", None, "/w")
    assert verdict.kind == tool_learning.OUTSIDE_WORKTREE
    assert _deny(_task(), None, tool="Read") == []


def test_a_declared_tool_is_asked_for_by_its_own_name(home, capsys) -> None:
    task_id = _task()
    assert main(["need", str(task_id), "--capability", "WebFetch", "--why", "read the docs"]) == 0
    [request] = cr.all_requests(init_db(), task_id=task_id)
    assert request.pattern == "WebFetch"


# (b) a program named by path: decided by reach, granted in the shape that runs


def test_the_worktrees_own_python_by_path_is_granted_as_that_path(home, worktree) -> None:
    task_id = _task()

    [request] = _deny(task_id, '.venv/bin/python -c "import app"', worktree=str(worktree))

    assert (request.program, request.pattern, request.state) == (
        "python",
        "Bash(.venv/bin/python:*)",
        cr.AUTO_GRANTED,
    )
    assert (request.path, request.reach) == (".venv/bin/python", cr.IN_WORKTREE)
    assert "Bash(.venv/bin/python:*)" in _launch_tools(task_id)
    # A path names one worktree's files: it is this task's, never every worker's.
    assert "Bash(.venv/bin/python:*)" not in load_config().claude.extra_tools
    assert "Bash(.venv/bin/python:*)" not in _launch_tools(_task())
    assert any("granted as `Bash(.venv/bin/python:*)`" in m for _t, m in home)


def test_a_venv_the_runtime_linked_to_the_base_clone_is_the_worktrees_own(home, tmp_path) -> None:
    base = tmp_path / "base"
    (base / ".venv" / "bin").mkdir(parents=True)
    (base / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
    root = tmp_path / "linked"
    root.mkdir()
    (root / ".venv").symlink_to(base / ".venv", target_is_directory=True)
    conn = init_db()
    repo_id = store.add_repo(
        conn,
        name="api",
        origin="https://github.com/acme/api",
        local_path=str(base),
        default_branch="main",
        base_sha=None,
    )
    task_id = store.add_task(conn, run_id=store.create_run(conn, "api"), title="t", repo_id=repo_id)
    store.set_task_status(conn, task_id, "in_progress")
    conn.close()

    [request] = _deny(task_id, ".venv/bin/python -m pytest", worktree=str(root))

    assert (request.reach, request.state) == (cr.IN_WORKTREE, cr.AUTO_GRANTED)


def test_an_absolute_path_waits_on_the_manager_with_the_path_in_the_request(home) -> None:
    task_id = _task()

    [request] = _deny(task_id, "/opt/tool/bin/thing x", worktree="/w")

    assert (request.state, request.reach, request.path) == (
        cr.PENDING,
        cr.ABSOLUTE,
        "/opt/tool/bin/thing",
    )
    assert request.pattern == "Bash(/opt/tool/bin/thing:*)"
    [problem] = [p for p in readiness.check().problems if p.code == cr.MANAGER_PROBLEM_CODE]
    assert "/opt/tool/bin/thing" in problem.summary and "absolute path" in problem.summary


def test_a_python_outside_the_worktree_is_never_granted_by_the_family(home, worktree) -> None:
    [request] = _deny(_task(), "../other/bin/python -c 1", worktree=str(worktree))

    assert (request.state, request.reach) == (cr.PENDING, cr.OUTSIDE)
    assert request.resolved and request.resolved.endswith("/other/bin/python")


def test_a_path_that_leaves_the_worktree_through_a_link_is_outside(
    home, worktree, tmp_path
) -> None:
    elsewhere = tmp_path / "elsewhere" / "bin"
    elsewhere.mkdir(parents=True)
    (elsewhere / "python").write_text("#!/bin/sh\n")
    (worktree / "tools").symlink_to(elsewhere, target_is_directory=True)

    [request] = _deny(_task(), "tools/python -c 1", worktree=str(worktree))

    assert (request.state, request.reach) == (cr.PENDING, cr.OUTSIDE)
    assert request.resolved == str((elsewhere / "python").resolve())


def test_a_dotdot_that_comes_back_inside_is_normalised_first(home, worktree) -> None:
    [request] = _deny(_task(), ".venv/../.venv/bin/python -c 1", worktree=str(worktree))
    assert (request.reach, request.state) == (cr.IN_WORKTREE, cr.AUTO_GRANTED)
    assert request.pattern == "Bash(.venv/../.venv/bin/python:*)"


def test_one_path_asked_twice_is_one_request_and_another_path_is_a_second(home, worktree) -> None:
    (worktree / "bin").mkdir()
    (worktree / "bin" / "python").write_text("#!/bin/sh\n")
    task_id = _task()

    _deny(task_id, ".venv/bin/python -c 1", worktree=str(worktree), use="a")
    _deny(task_id, ".venv/bin/python -c 2", worktree=str(worktree), use="b")
    found = _deny(task_id, "bin/python -c 3", worktree=str(worktree), use="c")

    assert [r.pattern for r in found] == ["Bash(.venv/bin/python:*)", "Bash(bin/python:*)"]


@pytest.mark.parametrize(
    ("command", "auto_grant", "never", "state"),
    [
        (".venv/bin/python -c 1", (), (), cr.AUTO_GRANTED),  # the family
        (".venv/bin/python -c 1", (), ("python",), cr.REFUSED),  # never beats the family
        (".venv/bin/python -c 1", ("python",), ("python",), cr.REFUSED),  # and auto_grant
        ("zig-out/bin/zig build", ("zig",), (), cr.AUTO_GRANTED),  # auto_grant beats pending
        ("zig-out/bin/zig build", (), (), cr.PENDING),  # nothing grants it: the manager's
    ],
)
def test_never_beats_auto_grant_beats_the_family_for_the_resolved_basename(
    home, worktree, command, auto_grant, never, state
) -> None:
    _policy(auto_grant, never)
    zig = worktree / "zig-out" / "bin"
    zig.mkdir(parents=True)
    (zig / "zig").write_text("#!/bin/sh\n")

    [request] = _deny(_task(), command, worktree=str(worktree))

    assert request.state == state


def test_a_path_is_never_granted_to_every_worker(home) -> None:
    [request] = _deny(_task(), "/opt/tool/bin/thing x", worktree="/w")

    with pytest.raises(cr.CapabilityError, match="without --always"):
        cr.decide_request(request.id, approve=True, always=True)
    decided = cr.decide_request(request.id, approve=True)

    assert decided.state == cr.GRANTED
    conn = init_db()
    assert cr.granted_patterns(conn, request.task_id) == ["Bash(/opt/tool/bin/thing:*)"]
    assert "Bash(/opt/tool/bin/thing:*)" not in load_config().claude.extra_tools


# A safe program refused for where or how it was used is not a grant (plan item 7)


def test_a_family_write_outside_the_worktree_is_outside_and_asks_nobody(home) -> None:
    task_id = _task()
    for use, command in (("a", "cp /tmp/note.txt .ppy-evidence/"), ("b", "mkdir /tmp/x")):
        assert _deny(task_id, command, worktree="/w", use=use) == []
        assert tool_learning.classify("Bash", command, "/w").kind == tool_learning.OUTSIDE_WORKTREE


def test_a_family_program_refused_for_its_arguments_waits_on_the_manager(home) -> None:
    task_id = _task()
    [request] = _deny(task_id, "rm -rf /w", worktree="/w")
    assert (request.program, request.state, request.reach) == ("rm", cr.PENDING, cr.ARGUMENTS)
    assert "Bash(rm:*)" not in load_config().claude.extra_tools
    # One the profile already has cannot be granted into anything: the ledger's, not a request.
    assert _deny(task_id, "find . -name '*.pyc' -delete", worktree="/w", use="t2") == [request]


# (d) a stack nothing in the runtime has heard of


def test_zig_goes_to_the_manager_then_the_launch_and_escalated_to_the_owner(home) -> None:
    from datetime import UTC, datetime

    from papaya_agent_runtime import outreach, papaya_events

    conn = init_db()
    run_id = store.create_run(conn, "ticket PAP-301")
    ticket = store.add_task(conn, run_id=run_id, title="ticket PAP-301")
    store.set_task_env(
        conn, ticket, papaya_events.PAPAYA_EVENT_METADATA, json.dumps({"work_item_id": "PAP-301"})
    )
    task_id = store.add_task(conn, run_id=run_id, title="build it in zig")
    store.set_task_status(conn, task_id, "in_progress")
    conn.close()

    [request] = _deny(task_id, "zig build test", worktree="/w")
    assert (request.program, request.pattern, request.state) == ("zig", "Bash(zig:*)", cr.PENDING)

    assert (
        main(["capability", "escalate", str(request.id), "--why", "needs the org's zig cache"]) == 0
    )
    conn = init_db()
    asks = [a for a in outreach.collect(conn) if a.kind == outreach.CAPABILITY]
    said = outreach.message(conn, asks, now=datetime.now(UTC), host="mac")
    assert "`zig`" in said and f"worker task {task_id}" in said and "[PAP-301]" in said
    assert f"ppy capability approve {request.id}" in said

    assert main(["capability", "approve", str(request.id)]) == 0
    assert "Bash(zig:*)" in _launch_tools(task_id)
    assert "Bash(zig:*)" not in load_config().claude.extra_tools
