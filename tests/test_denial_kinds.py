"""A denied command is a command-shape denial, a policy refusal, or a gap in the profile.

The self-report's first night opened two wrong issues: "denied `Bash(cd:*)`" for
`cd <worktree> && git show`, which the profile allows and the command rules refuse for
its `&&`, and "denied `Bash(docker:*)`" for a worker querying a container the
environment block says is not its own. Only a gap in the profile is the runtime's to
report; the others are the worker's to be told about.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import test_serve
from papaya_agent_runtime import capability_requests, deficiencies, tool_learning
from papaya_agent_runtime.config import MMConfig, load_config, save_config
from papaya_agent_runtime.providers.base import ProviderEvent
from papaya_agent_runtime.providers.claude import ClaudeAdapter
from papaya_agent_runtime.providers.command_rules import FLAGGED_RULE, command_rules
from papaya_agent_runtime.state import store
from papaya_agent_runtime.state.db import init_db
from test_deficiencies import FakeGh, _reporter, _serve_with
from test_serve import EVENT, FakeEvents, FakePapaya, Harness, _deliver, _review_ticket, _runner

globals().update(
    {name: getattr(test_serve, name) for name in ("client_home", "ready", "registered_repo")}
)

WORKTREE = "/tmp/ppy-worktrees/task-7"
BRANCH = "ppy/task-7-abc"


def _denial(command: str, use: str, **refusal: Any) -> dict[str, Any]:
    """A denial as the adapter hands it over, with the refusal evidence it carries.

    The default is the harness's own refusal line, which is what a live
    `permission_denied` always is: that line IS the harness saying it refused.
    """
    said = {
        "harness_line": True,
        "decision_reason_type": "",
        "decision_reason": "",
        "message": "",
        "tool_result": "",
    }
    said.update(refusal)
    return {
        "tool_name": "Bash",
        "tool_use_id": use,
        "tool_input": {"command": command},
        "refusal": said,
    }


@pytest.fixture
def steers(monkeypatch) -> list[tuple[int, str]]:
    sent: list[tuple[int, str]] = []
    monkeypatch.setattr(tool_learning, "steer_worker", lambda task, msg: sent.append((task, msg)))
    return sent


def _tasks(repo: str, n: int) -> tuple[int, list[int]]:
    conn = init_db()
    run_id = store.create_run(conn, "run")
    repo_id = store.add_repo(
        conn,
        name=repo,
        origin=f"https://github.com/acme/{repo}",
        local_path="/tmp/x",
        default_branch="main",
        base_sha=None,
    )
    tasks = [store.add_task(conn, run_id=run_id, title="w", repo_id=repo_id) for _ in range(n)]
    conn.close()
    return run_id, tasks


def _learn(task_id: int, run_id: int, *denials: dict[str, Any]) -> list[dict]:
    return tool_learning.learn(
        list(denials), task_id=task_id, run_id=run_id, worktree=WORKTREE, branch=BRANCH
    )


def _recorded(task_id: int | None = None) -> list[dict[str, Any]]:
    rows = (
        init_db()
        .execute(
            "SELECT task_id, payload FROM events WHERE kind = ? ORDER BY id",
            (tool_learning.PERMISSION_DENIED,),
        )
        .fetchall()
    )
    return [json.loads(r["payload"]) for r in rows if task_id is None or r["task_id"] == task_id]


# ── the kinds ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("command", "kind"),
    [
        ("cd /tmp/ppy-worktrees/task-7 && grep -n needle src/app.py", "command_shape"),
        ("git status | tail -5", "command_shape"),
        ("FOO=1 python3 -c 'print(1)'", "command_shape"),
        ("uv run pytest > out.txt", "command_shape"),
        ("docker compose up -d", "policy_refusal"),
        ("sudo make install", "policy_refusal"),
        ("python3 -c 'print(1)'", "profile_gap"),
        ("jq . package.json", "profile_gap"),
    ],
)
def test_each_shape_classifies_to_its_kind(command: str, kind: str) -> None:
    assert tool_learning.classify("Bash", command, WORKTREE).kind == kind


# ── one denial, one record ──────────────────────────────────────────────────


def test_three_identical_denials_in_one_second_record_once(ppy_home, steers) -> None:
    run_id, (task,) = _tasks("api", 1)
    same = _denial("terraform plan", "toolu_1")

    # Reported live, retried as it was under a new tool call id, and listed again
    # by the turn's result at the end.
    _learn(task, run_id, same)
    _learn(task, run_id, _denial("terraform plan", "toolu_2"))
    _learn(task, run_id, same, _denial("terraform plan", "toolu_2"))

    (only,) = _recorded(task)
    assert (only["kind"], only["tool_use_id"]) == (tool_learning.PROFILE_GAP, "toolu_1")
    # One request carries it to the manager; a denial the loop carries is not an issue.
    conn = init_db()
    assert [r.program for r in capability_requests.all_requests(conn, task_id=task)] == [
        "terraform"
    ]
    assert deficiencies.ledger(include_all=True) == []


def test_a_live_permission_denied_line_carries_the_command_its_tool_use_ran() -> None:
    command = "cd /tmp/wt && git status --short"
    events = [
        ProviderEvent(
            kind="assistant",
            raw={
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_9",
                            "name": "Bash",
                            "input": {"command": command},
                        }
                    ]
                },
            },
        ),
        ProviderEvent(
            kind="system",
            raw={
                "type": "system",
                "subtype": "permission_denied",
                "tool_name": "Bash",
                "tool_use_id": "toolu_9",
            },
        ),
    ]
    adapter = ClaudeAdapter()
    assert adapter.live_denial(events[0], events[:1]) is None
    assert adapter.live_denial(events[1], events) == _denial(command, "toolu_9")


def test_a_live_denial_whose_line_carries_a_refusal_message_string_is_read() -> None:
    """The harness's permission_denied line has a string ``message``; tasks 22 and 23's
    runners crashed reading it as an object on their first denial (2026-09-17)."""
    command = "find . -maxdepth 1 -type d"
    events = [
        ProviderEvent(
            kind="assistant",
            raw={
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_7",
                            "name": "Bash",
                            "input": {"command": command},
                        }
                    ]
                },
            },
        ),
        ProviderEvent(kind="system", raw={"type": "system", "message": "status text"}),
        ProviderEvent(
            kind="system",
            raw={
                "type": "system",
                "subtype": "permission_denied",
                "tool_name": "Bash",
                "tool_use_id": "toolu_7",
                "decision_reason_type": "subcommandResults",
                "message": "This Bash command contains multiple operations.",
            },
        ),
    ]
    adapter = ClaudeAdapter()
    assert adapter.live_denial(events[2], events) == _denial(
        command,
        "toolu_7",
        decision_reason_type="subcommandResults",
        message="This Bash command contains multiple operations.",
    )


# ── steering the worker ─────────────────────────────────────────────────────


def test_two_command_shape_denials_steer_once_with_the_rules_and_a_third_says_nothing(
    ppy_home, steers
) -> None:
    run_id, (task,) = _tasks("api", 1)

    _learn(task, run_id, _denial("cd src && ls", "toolu_1"))
    assert steers == []

    _learn(task, run_id, _denial("git log | head -3", "toolu_2"))
    ((steered, message),) = steers
    assert steered == task
    assert command_rules("claude", BRANCH) in message
    assert "`cd src && ls`" in message and "`git log | head -3`" in message

    _learn(task, run_id, _denial("ls > files.txt", "toolu_3"))
    assert len(steers) == 1


def test_a_policy_refusal_steers_once_with_the_rule_it_broke(ppy_home, steers) -> None:
    run_id, (task,) = _tasks("api", 1)
    psql = 'docker exec db-1 psql -U app -c "\\d handoffs"'

    _learn(task, run_id, _denial(psql, "toolu_1"))
    _learn(task, run_id, _denial("docker ps", "toolu_2"))

    ((_, message),) = steers
    assert tool_learning.policy_rule("docker") in message
    assert FLAGGED_RULE in message
    assert psql in message


# ── what reaches an issue ───────────────────────────────────────────────────


def test_only_a_profile_gap_learns_or_opens_an_issue(ppy_home, steers) -> None:
    cfg = MMConfig()
    cfg.claude.dropped_tools = ["Bash(python3:*)"]
    save_config(cfg)
    run_id, tasks = _tasks("api", 2)

    for n, task in enumerate(tasks):
        _learn(
            task,
            run_id,
            _denial("python3 -c 'print(1)'", f"toolu_{n}a"),
            _denial(f"terraform plan -var n={n}", f"toolu_{n}b"),
            _denial(f"cd src && terraform plan -var n={n}", f"toolu_{n}c"),
            _denial(f"docker ps -a --filter n={n}", f"toolu_{n}d"),
            _denial(f"sudo ls /root/{n}", f"toolu_{n}e"),
            # Allowed by the profile and refused anyway: nothing carries it but the ledger.
            _denial(f"git status --short n{n}", f"toolu_{n}f"),
        )
    gh = FakeGh()
    _reporter(gh).flush()

    assert "Bash(python3:*)" not in load_config().claude.dropped_tools
    (issue,) = gh.created()
    assert issue["labels"] == [deficiencies.LABEL, deficiencies.WORKER_DENIAL]
    assert "Bash(git:*)" in issue["title"]
    assert "already in the worker profile" in issue["body"]
    # The missing program went to the manager instead, once per task.
    conn = init_db()
    assert {r.program for r in capability_requests.pending(conn)} == {"terraform"}
    kinds = {d.kind for d in deficiencies.ledger(include_all=True)}
    assert kinds == {deficiencies.WORKER_DENIAL, deficiencies.PROMPT_CLARITY}
    assert tool_learning.counts()["api"] == {
        tool_learning.PROFILE_GAP: 6,
        tool_learning.COMMAND_SHAPE: 2,
        tool_learning.POLICY_REFUSAL: 4,
    }


def test_three_workers_breaking_the_rules_in_one_repository_in_a_day_are_one_issue(
    ppy_home, steers
) -> None:
    run_id, tasks = _tasks("api", 3)
    for n, task in enumerate(tasks[:2]):
        _learn(
            task, run_id, _denial(f"cd src && ls {n}", f"a{n}"), _denial(f"ls | wc {n}", f"b{n}")
        )
    (row,) = deficiencies.ledger(include_all=True)
    assert (row.kind, row.count, row.status) == (
        deficiencies.PROMPT_CLARITY,
        2,
        deficiencies.WATCHING,
    )

    _learn(tasks[2], run_id, _denial("git diff > d.txt", "c"))
    gh = FakeGh()
    _reporter(gh).flush()

    (issue,) = gh.created()
    assert issue["labels"] == [deficiencies.LABEL, deficiencies.PROMPT_CLARITY]
    for command in ("cd src && ls 0", "cd src && ls 1", "git diff > d.txt"):
        assert f"command `{command}`" in issue["body"]


# ── issues an older classifier opened ───────────────────────────────────────


def _old_denial_issue(task: int, run_id: int, pattern: str, commands: list[str]) -> None:
    """A `worker-denial` row and its events as the classifier before kinds wrote them."""
    conn = init_db()
    for command in commands:
        store.append_event(
            conn,
            kind=tool_learning.PERMISSION_DENIED,
            payload={"tool": "Bash", "command": command, "pattern": pattern, "in_family": False},
            run_id=run_id,
            task_id=task,
        )
    conn.close()
    for _ in commands:
        deficiencies.record(
            deficiencies.WORKER_DENIAL,
            f"`{pattern}`",
            evidence={"pattern": pattern, "repo": "runtime", "task_id": task, "run_id": run_id},
            scope="repo:runtime",
        )


def test_serve_start_comments_on_and_closes_an_issue_whose_denials_changed_kind(
    ppy_home, client_home, ready, registered_repo
) -> None:
    conn = init_db()
    run_id = store.create_run(conn, "earlier")
    task = store.add_task(conn, run_id=run_id, title="earlier worker")
    conn.close()
    _old_denial_issue(task, run_id, "Bash(cd:*)", ["cd /wt && git status", "cd /wt && git show"])
    _old_denial_issue(
        task,
        run_id,
        "Bash(docker:*)",
        ['docker exec db psql -c "\\d t" 2>&1 | head -40', 'docker exec db psql -c "\\d t"'],
    )
    _old_denial_issue(task, run_id, "Bash(terraform:*)", ["terraform plan", "terraform apply"])
    gh = FakeGh()
    reporter = _reporter(gh)
    reporter.flush()
    url = {issue["title"].split(": ")[-1]: u for u, issue in gh.issues.items()}
    cd, docker, terraform = (url[f"`Bash({p}:*)`"] for p in ("cd", "docker", "terraform"))
    assert all(issue["state"] == "OPEN" for issue in gh.issues.values())

    def delivered(turn):
        _deliver(turn)
        return "Approved and delivered."

    papaya_api = FakePapaya()
    runner = _runner(_review_ticket(delivered, papaya_api), papaya_api)
    assert _serve_with(Harness(FakeEvents([EVENT])), client_home, runner, reporter) == 0
    assert reporter.reclassify() == []  # the next start finds nothing more to say

    assert gh.issues[cd]["state"] == "CLOSED"
    (comment,) = gh.issues[cd]["comments"]
    assert comment.startswith("re-classified as command_shape; closing")
    # The exact rewrite, not the rule in general.
    assert "Instead: `git status`, on its own" in comment
    assert gh.issues[docker]["state"] == "CLOSED"
    (comment,) = gh.issues[docker]["comments"]
    assert comment.startswith("re-classified as command_shape/policy_refusal; closing")
    # A missing program is a request the manager decides now, not an issue.
    assert gh.issues[terraform]["state"] == "CLOSED"
    (comment,) = gh.issues[terraform]["comments"]
    assert comment.startswith("re-classified as capability_request; closing")
    assert "ppy capability approve <id>" in comment and "ppy capability escalate" in comment
    statuses = {d.detail: d.status for d in deficiencies.ledger(include_all=True)}
    assert set(statuses.values()) == {deficiencies.RECLASSIFIED}
    assert deficiencies.ledger() == []
