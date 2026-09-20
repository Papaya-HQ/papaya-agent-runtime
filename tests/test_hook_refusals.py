"""A repository's own hook refused it, and the runtime says so instead of guessing.

Fourteen times (issue #83, plus #116 on a teammate's machine) a worker finished,
ran exactly the `git push origin HEAD:ppy/task-<n>-<id>` its rules prescribe, and
`.claude/hooks/verify-before-push.sh` refused it. The runtime recorded
``kind: profile_gap``, ``reason: "git is not in the safe family"`` — false twice
over: `Bash(git:*)` is in `config.CLAUDE_PROFILE`, and the profile never saw the
call. It pointed maintainers at widening the profile, which would have changed
nothing at all.

Every stream shape in this module is copied from a real recorded denial on the
live state database, read 2026-09-20. The two shapes are:

- the HARNESS refusing: a `{"type": "system", "subtype": "permission_denied"}`
  line naming the tool call, carrying its own words;
- a HOOK refusing: no such line anywhere in the turn, and an error `tool_result`
  holding the hook's stderr.

Six denials on record have no harness line, and all six are that push.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from papaya_agent_runtime import deficiencies, tool_learning
from papaya_agent_runtime.providers.base import ProviderEvent
from papaya_agent_runtime.providers.claude import ClaudeAdapter, registered_hooks
from papaya_agent_runtime.state import store
from papaya_agent_runtime.state.db import init_db

PUSH = "git push origin HEAD:ppy/task-112-53ebeb920815"

#: Verbatim from task 112's recorded `tool_result` (papaya-backend-monorepo).
HOOK_STDERR = (
    "Error: PreToolUse:Bash hook error: make verify failed — fix issues before pushing.\n"
    "\n"
    "Last 40 lines of /tmp log /var/folders/yh/T/papaya-verify.XXXXXX.log.DhauJCy3RE:\n"
    "\n"
    "Harness check failed:\n"
    "- large source file is untracked: backend/app/api/v1/routes/documents.py has 1004 "
    "lines; add it to docs/exec-plans/tech-debt-tracker.md\n"
    "make: *** [harness-check] Error 1"
)

#: Verbatim from task 129's, on an older Claude Code: the same hook, no prefix.
HOOK_STDERR_UNPREFIXED = (
    "Error: make verify failed — fix issues before pushing.\n"
    "\n"
    "Last 40 lines of /tmp log /var/folders/yh/T/papaya-verify.XXXXXX.log.Bukppvz1Fw:\n"
    "make: *** [frontend-browser-smoke] Error 1"
)

#: Verbatim from a recorded working-directory refusal: the harness's own wording.
HARNESS_BLOCKED = (
    "ls in '/Users/someone/elsewhere' was blocked. For security, Claude Code may only "
    "list files in the allowed working directories for this session: '/wt'."
)

SETTINGS = {
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/verify-before-push.sh",
                        "timeout": 600,
                        "statusMessage": "Running make verify before push…",
                    }
                ],
            }
        ]
    }
}


@pytest.fixture
def worktree(tmp_path):
    """A worktree that registers the hook, exactly as the two monorepos do."""
    hooks = tmp_path / ".claude"
    hooks.mkdir()
    (hooks / "settings.json").write_text(json.dumps(SETTINGS), encoding="utf-8")
    return tmp_path


def _tool_use(command: str, use: str) -> ProviderEvent:
    return ProviderEvent(
        kind="assistant",
        raw={
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": use, "name": "Bash", "input": {"command": command}}
                ]
            },
        },
    )


def _hook_block(use: str, said: str = HOOK_STDERR) -> ProviderEvent:
    """The shape a PreToolUse hook's refusal takes: an error result, no system line."""
    return ProviderEvent(
        kind="user",
        raw={
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": use,
                        "content": said,
                        "is_error": True,
                    }
                ]
            },
            "tool_result_meta": [{"id": use, "non_execution_kind": "permission-rule"}],
        },
    )


def _harness_line(use: str, **extra: Any) -> ProviderEvent:
    return ProviderEvent(
        kind="system",
        raw={
            "type": "system",
            "subtype": "permission_denied",
            "tool_name": "Bash",
            "tool_use_id": use,
            **extra,
        },
    )


def _result(*uses: tuple[str, str]) -> ProviderEvent:
    return ProviderEvent(
        kind="result",
        raw={
            "type": "result",
            "subtype": "success",
            "permission_denials": [
                {
                    "tool_name": "Bash",
                    "tool_use_id": use,
                    "tool_input": {"command": command},
                }
                for use, command in uses
            ],
        },
    )


# ── telling the two shapes apart ────────────────────────────────────────────


def test_a_hook_block_is_a_hook_refusal_and_never_a_profile_gap(worktree) -> None:
    events = [_tool_use(PUSH, "toolu_1"), _hook_block("toolu_1"), _result(("toolu_1", PUSH))]

    (denial,) = ClaudeAdapter().permission_denials(events)
    verdict = tool_learning.classify("Bash", PUSH, str(worktree), refusal=denial["refusal"])

    assert denial["refusal"]["harness_line"] is False
    assert verdict.kind == tool_learning.HOOK_REFUSAL
    assert verdict.hook == ".claude/hooks/verify-before-push.sh"
    assert verdict.hook_settings == ".claude/settings.json"
    assert verdict.inferred is False
    assert "is not in the safe family" not in verdict.reason
    assert ".claude/hooks/verify-before-push.sh" in verdict.reason
    assert ".claude/settings.json" in verdict.reason


def test_an_older_harness_that_omits_the_hook_prefix_is_read_the_same_way(worktree) -> None:
    """Tasks 58 and 129 carry the hook's stderr with no `PreToolUse:` prefix.

    The prefix is corroboration, never the test — the absence of the harness's own
    refusal line is. Keying on the prefix would have missed a third of the real ones.
    """
    events = [
        _tool_use(PUSH, "toolu_2"),
        _hook_block("toolu_2", HOOK_STDERR_UNPREFIXED),
        _result(("toolu_2", PUSH)),
    ]

    (denial,) = ClaudeAdapter().permission_denials(events)

    assert (
        tool_learning.classify("Bash", PUSH, str(worktree), refusal=denial["refusal"]).kind
        == tool_learning.HOOK_REFUSAL
    )


def test_a_harness_refusal_of_the_same_command_is_not_a_hook_refusal(worktree) -> None:
    """The discriminator has to work in both directions, in the same repository.

    This worktree registers the hook. A call the HARNESS refused still carries its
    own `permission_denied` line, so it is judged on the command as it always was.
    """
    events = [
        _tool_use(PUSH, "toolu_3"),
        _harness_line("toolu_3", message="This command requires approval"),
        _result(("toolu_3", PUSH)),
    ]

    (denial,) = ClaudeAdapter().permission_denials(events)
    verdict = tool_learning.classify("Bash", PUSH, str(worktree), refusal=denial["refusal"])

    assert denial["refusal"]["harness_line"] is True
    assert verdict.kind == tool_learning.PROFILE_GAP


def test_a_working_directory_block_is_the_harness_even_with_no_system_line(worktree) -> None:
    """A tool_result in the harness's own words is the harness, not a hook.

    17 recorded `outside_worktree` denials read back this way. Treating "no system
    line" alone as a hook would relabel every one of them.
    """
    command = "ls /Users/someone/elsewhere"
    events = [
        _tool_use(command, "toolu_4"),
        _hook_block("toolu_4", HARNESS_BLOCKED),
        _result(("toolu_4", command)),
    ]

    (denial,) = ClaudeAdapter().permission_denials(events)
    verdict = tool_learning.classify("Bash", command, str(worktree), refusal=denial["refusal"])

    assert denial["refusal"]["harness_line"] is True
    assert verdict.kind == tool_learning.OUTSIDE_WORKTREE


def test_a_denial_with_no_evidence_at_all_is_judged_on_the_command(worktree) -> None:
    """No line and no result is no evidence, not evidence of a hook.

    A denial read back from a bare `result` event has neither. Claiming a hook there
    would put a false diagnosis on the common case, which is this change's own
    failure mode.
    """
    events = [_result(("toolu_5", "terraform plan"))]

    (denial,) = ClaudeAdapter().permission_denials(events)
    verdict = tool_learning.classify(
        "Bash", "terraform plan", str(worktree), refusal=denial["refusal"]
    )

    assert denial["refusal"]["harness_line"] is True
    assert verdict.kind == tool_learning.PROFILE_GAP


def test_a_hook_that_allows_the_push_is_recorded_as_nothing_at_all(worktree) -> None:
    """The negative case the brief names: a hook exiting 0 is not a denial.

    A hook that allows the call produces no `permission_denials` entry, so there is
    nothing to classify and nothing is recorded.
    """
    events = [
        _tool_use(PUSH, "toolu_6"),
        ProviderEvent(
            kind="user",
            raw={
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_6", "content": "main -> main"}
                    ]
                },
            },
        ),
        ProviderEvent(kind="result", raw={"type": "result", "subtype": "success"}),
    ]

    assert ClaudeAdapter().permission_denials(events) == []


def test_a_repository_with_no_readable_hook_says_the_diagnosis_is_inferred(tmp_path) -> None:
    """H1's fallback: no hook could be read, so the word `inferred` is on the record.

    The call still got past the harness, so a hook is still the only thing left —
    but which hook is a deduction, and it says so rather than naming one.
    """
    events = [_tool_use(PUSH, "toolu_7"), _hook_block("toolu_7"), _result(("toolu_7", PUSH))]

    (denial,) = ClaudeAdapter().permission_denials(events)
    verdict = tool_learning.classify("Bash", PUSH, str(tmp_path), refusal=denial["refusal"])

    assert verdict.kind == tool_learning.HOOK_REFUSAL
    assert verdict.inferred is True
    assert verdict.hook == ""
    assert "inferred" in verdict.reason


def test_a_hook_matcher_is_read_the_way_claude_code_reads_it(tmp_path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Write|Edit", "hooks": [{"command": "./a.sh"}]},
                        {"matcher": "*", "hooks": [{"command": "./b.sh"}]},
                        {"hooks": [{"command": "./c.sh"}]},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    scripts = [h["script"] for h in registered_hooks(str(tmp_path), "Bash")]

    assert scripts == ["b.sh", "c.sh"]
    assert [h["script"] for h in registered_hooks(str(tmp_path), "Edit")] == [
        "a.sh",
        "b.sh",
        "c.sh",
    ]


def test_an_unreadable_settings_file_never_raises(tmp_path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")

    assert registered_hooks(str(tmp_path), "Bash") == []
    assert registered_hooks(None, "Bash") == []


# ── what the record and the ledger say ──────────────────────────────────────


def _task(worktree) -> tuple[int, int]:
    conn = init_db()
    run_id = store.create_run(conn, "run")
    repo_id = store.add_repo(
        conn,
        name="papaya-backend-monorepo",
        origin="https://github.com/acme/backend",
        local_path=str(worktree),
        default_branch="main",
        base_sha=None,
    )
    task_id = store.add_task(conn, run_id=run_id, title="w", repo_id=repo_id)
    conn.close()
    return run_id, task_id


def _recorded(task_id: int) -> list[dict[str, Any]]:
    rows = (
        init_db()
        .execute(
            "SELECT payload FROM events WHERE kind = ? AND task_id = ? ORDER BY id",
            (tool_learning.PERMISSION_DENIED, task_id),
        )
        .fetchall()
    )
    return [json.loads(r["payload"]) for r in rows]


@pytest.fixture
def steers(monkeypatch) -> list[tuple[int, str]]:
    sent: list[tuple[int, str]] = []
    monkeypatch.setattr(tool_learning, "steer_worker", lambda task, msg: sent.append((task, msg)))
    return sent


def test_the_recorded_reason_names_the_hook_and_carries_its_own_words(worktree, steers) -> None:
    run_id, task_id = _task(worktree)
    events = [_tool_use(PUSH, "toolu_1"), _hook_block("toolu_1"), _result(("toolu_1", PUSH))]

    tool_learning.learn(
        ClaudeAdapter().permission_denials(events),
        task_id=task_id,
        run_id=run_id,
        worktree=str(worktree),
        branch="ppy/task-112-53ebeb920815",
    )

    (found,) = _recorded(task_id)
    assert found["kind"] == tool_learning.HOOK_REFUSAL
    assert found["hook"] == ".claude/hooks/verify-before-push.sh"
    assert found["inferred"] is False
    assert "is not in the safe family" not in found["reason"]
    # The hook's own words, with the harness's prefix off so both releases read alike.
    assert found["hook_said"].startswith("make verify failed")
    assert "documents.py has 1004 lines" in found["hook_said"]


def test_the_worker_is_told_the_runtime_pushes_rather_than_to_retry(worktree, steers) -> None:
    run_id, task_id = _task(worktree)
    events = [_tool_use(PUSH, "toolu_1"), _hook_block("toolu_1"), _result(("toolu_1", PUSH))]

    tool_learning.learn(
        ClaudeAdapter().permission_denials(events),
        task_id=task_id,
        run_id=run_id,
        worktree=str(worktree),
        branch="ppy/task-112-53ebeb920815",
    )

    (_, message) = steers[0]
    assert ".claude/hooks/verify-before-push.sh" in message
    assert "make verify failed" in message
    assert "Do not push in this repository; the runtime pushes for you." in message
    assert "--no-verify" in message  # said as forbidden, never as a way through


def test_one_steer_per_hook_however_many_commands_it_refuses(worktree, steers) -> None:
    run_id, task_id = _task(worktree)
    for i, command in enumerate((PUSH, "git push origin HEAD:other", PUSH)):
        use = f"toolu_{i}"
        events = [_tool_use(command, use), _hook_block(use), _result((use, command))]
        tool_learning.learn(
            ClaudeAdapter().permission_denials(events),
            task_id=task_id,
            run_id=run_id,
            worktree=str(worktree),
            branch="b",
        )

    assert len(steers) == 1


def test_the_ledger_raises_a_hook_deficiency_not_a_worker_denial(worktree, steers) -> None:
    run_id, task_id = _task(worktree)
    for i in range(2):
        use = f"toolu_{i}"
        events = [_tool_use(PUSH, use), _hook_block(use), _result((use, PUSH))]
        tool_learning.learn(
            ClaudeAdapter().permission_denials(events),
            task_id=task_id,
            run_id=run_id,
            worktree=str(worktree),
            branch="b",
        )

    kinds = {row.kind for row in deficiencies.ledger(include_all=True)}
    assert deficiencies.WORKER_DENIAL_HOOK in kinds
    assert deficiencies.WORKER_DENIAL not in kinds
    (row,) = [
        r
        for r in deficiencies.ledger(include_all=True)
        if r.kind == deficiencies.WORKER_DENIAL_HOOK
    ]
    assert ".claude/hooks/verify-before-push.sh" in row.detail
    assert any("documents.py has 1004 lines" in str(e.get("hook_said")) for e in row.evidence)


def test_a_hook_refusal_never_becomes_a_capability_request(worktree, steers, monkeypatch) -> None:
    """The refusal is not a missing program, so nobody is asked to grant `git`."""
    from papaya_agent_runtime import capability_requests

    asked: list[str] = []
    monkeypatch.setattr(
        capability_requests,
        "request",
        lambda *a, **kw: asked.append(a[1] if len(a) > 1 else ""),
    )
    run_id, task_id = _task(worktree)
    events = [_tool_use(PUSH, "toolu_1"), _hook_block("toolu_1"), _result(("toolu_1", PUSH))]

    tool_learning.learn(
        ClaudeAdapter().permission_denials(events),
        task_id=task_id,
        run_id=run_id,
        worktree=str(worktree),
        branch="b",
    )

    assert asked == []


def test_a_hook_refusal_teaches_the_profile_nothing(worktree, steers) -> None:
    """No pattern is learned and no tool is added: the profile was never the problem."""
    run_id, task_id = _task(worktree)
    events = [_tool_use(PUSH, "toolu_1"), _hook_block("toolu_1"), _result(("toolu_1", PUSH))]

    tool_learning.learn(
        ClaudeAdapter().permission_denials(events),
        task_id=task_id,
        run_id=run_id,
        worktree=str(worktree),
        branch="b",
    )

    assert tool_learning.learnable() == []
    assert tool_learning.refused() == []
