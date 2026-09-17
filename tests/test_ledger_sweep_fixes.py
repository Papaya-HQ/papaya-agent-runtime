"""Five defects found while working the ledger on 2026-09-17, each pinned here.

They share no code, only the day they cost something: a crash in the team view, a
check-in that fired the moment a session started, a reference repository a worker
could not read, a refusal learned as the wrong lesson, and a session that could not
be resumed at all.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from papaya_agent_runtime import health, rounds, tool_learning
from papaya_agent_runtime.providers.base import TaskSpec
from papaya_agent_runtime.providers.claude import ClaudeAdapter


def test_humanize_takes_a_float_without_crashing() -> None:
    """`running_seconds` is a float, and the team view formats it (2026-09-17)."""
    assert health.humanize(7501.4) == "2h05m"
    assert health.humanize(7501) == "2h05m"
    assert health.humanize(45.9) == "45s"
    assert health.humanize(None) == "never heard from"


def _look(**kw) -> rounds.WorkerLook:
    base = dict(
        task_id=1,
        status="in_progress",
        branch="b",
        created_at=None,
        verdict="alive",
        silent_seconds=0,
        last_event_id=0,
        progress=[],
        question=None,
        stopped=None,
        last_acted_id=0,
    )
    base.update(kw)
    return rounds.WorkerLook(**base)


def test_a_check_in_measures_this_session_not_the_age_of_the_task() -> None:
    """A fresh session on an old task is minutes old, not hours (task 30)."""
    now = datetime.now(UTC)
    look = _look(
        created_at=now - timedelta(hours=8),
        session_started_at=now - timedelta(minutes=3),
    )
    assert look.running_seconds(now) == 180.0
    # With no session recorded, the task's own age is still the best answer.
    assert _look(created_at=now - timedelta(minutes=10)).running_seconds(now) == 600.0


def test_dispatch_and_resume_both_start_a_session() -> None:
    assert rounds.SESSION_START_KINDS == ("dispatched", "resumed")


def test_a_read_pointed_outside_the_worktree_is_not_a_missing_tool() -> None:
    """`ls ../other-repo` is a directory boundary, not a gap in the profile."""
    verdict = tool_learning.classify("Bash", "ls /elsewhere/other-repo", "/work/repo")
    assert verdict.kind == tool_learning.OUTSIDE_WORKTREE
    assert verdict.in_family is False
    assert verdict.pattern == ""
    assert "ppy reference grant" in verdict.reason


def test_a_read_inside_the_worktree_is_still_learned_normally() -> None:
    verdict = tool_learning.classify("Bash", "ls src", "/work/repo")
    assert verdict.kind == tool_learning.PROFILE_GAP
    assert verdict.in_family is True
    assert verdict.pattern == "Bash(ls:*)"


def test_a_read_naming_no_path_at_all_is_still_learned_normally() -> None:
    verdict = tool_learning.classify("Bash", "ls -la", "/work/repo")
    assert verdict.in_family is True
    assert verdict.kind == tool_learning.PROFILE_GAP


def _spec(**kw) -> TaskSpec:
    base = dict(
        task_id=1,
        title="t",
        instructions="do the thing",
        worktree_path="/work/repo",
        base_sha="abc",
        provider="claude",
    )
    base.update(kw)
    return TaskSpec(**base)


def test_a_reference_repo_is_readable_and_never_writable(monkeypatch) -> None:
    monkeypatch.setenv("PPY_CLAUDE_ALLOWED_TOOLS", "Bash(ls:*)")
    argv = ClaudeAdapter().start(_spec(read_only_dirs=["/clones/other-repo"]))
    assert "--add-dir" in argv
    assert argv[argv.index("--add-dir") + 1] == "/clones/other-repo"
    denied = argv[argv.index("--disallowedTools") + 1]
    assert "Edit(/clones/other-repo/**)" in denied
    assert "Write(/clones/other-repo/**)" in denied
    assert "NotebookEdit(/clones/other-repo/**)" in denied


def test_without_a_reference_repo_nothing_changes(monkeypatch) -> None:
    monkeypatch.setenv("PPY_CLAUDE_ALLOWED_TOOLS", "Bash(ls:*)")
    argv = ClaudeAdapter().start(_spec())
    assert "--add-dir" not in argv


def _transcript(tmp_path, monkeypatch, worktree, session_id, entries) -> None:
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
    from papaya_agent_runtime.providers import claude as claude_mod

    path = claude_mod._transcript_path(str(worktree), session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def test_a_session_ending_on_an_assistant_turn_starts_fresh(tmp_path, monkeypatch) -> None:
    """`--resume` on such a transcript is a 400, which fails the task (task 30)."""
    monkeypatch.setenv("PPY_CLAUDE_ALLOWED_TOOLS", "Bash(ls:*)")
    worktree = tmp_path / "work"
    worktree.mkdir()
    _transcript(
        tmp_path,
        monkeypatch,
        worktree,
        "sess-1",
        [{"type": "user"}, {"type": "assistant"}],
    )
    argv = ClaudeAdapter().resume(
        _spec(
            worktree_path=str(worktree),
            resume_session_id="sess-1",
            steer_message="rebase onto main",
        )
    )
    assert "--resume" not in argv
    prompt = argv[argv.index("-p") + 1]
    assert "continuing an interrupted session" in prompt
    assert "rebase onto main" in prompt
    assert "do the thing" in prompt


def test_a_session_ending_on_a_user_turn_resumes_as_before(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PPY_CLAUDE_ALLOWED_TOOLS", "Bash(ls:*)")
    worktree = tmp_path / "work"
    worktree.mkdir()
    _transcript(
        tmp_path,
        monkeypatch,
        worktree,
        "sess-2",
        [{"type": "assistant"}, {"type": "user"}],
    )
    argv = ClaudeAdapter().resume(
        _spec(worktree_path=str(worktree), resume_session_id="sess-2", steer_message="carry on")
    )
    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == "sess-2"


def test_an_unreadable_transcript_resumes_rather_than_guessing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PPY_CLAUDE_ALLOWED_TOOLS", "Bash(ls:*)")
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
    argv = ClaudeAdapter().resume(_spec(resume_session_id="never-written", steer_message="go"))
    assert "--resume" in argv


def test_a_reference_repo_is_recorded_on_the_task_and_resolves_to_its_clone(
    ppy_home, source_repo
) -> None:
    """The grant is durable, so every later launch of the task carries it too."""
    from papaya_agent_runtime import repos
    from papaya_agent_runtime.state import init_db, store
    from papaya_agent_runtime.supervisor import core

    added = repos.add_repo(source_repo)
    conn = init_db()
    run_id = store.create_run(conn, "r")
    task_id = store.add_task(conn, run_id=run_id, title="t", provider="fake")

    assert core.reference_repo_names(conn, task_id) == []
    assert core.grant_reference_repos(conn, task_id, [added.name]) == [added.name]
    # Granting the same one twice leaves one entry, not two.
    assert core.grant_reference_repos(conn, task_id, [added.name]) == [added.name]
    assert core._reference_dirs(task_id) == [store.get_repo(conn, added.name)["local_path"]]
    conn.close()


def test_a_reference_repo_nobody_registered_is_refused(ppy_home) -> None:
    from papaya_agent_runtime.state import init_db, store
    from papaya_agent_runtime.supervisor import core

    conn = init_db()
    run_id = store.create_run(conn, "r")
    task_id = store.add_task(conn, run_id=run_id, title="t", provider="fake")
    try:
        core.grant_reference_repos(conn, task_id, ["not-registered"])
    except core.SupervisorError as exc:
        assert "not registered" in str(exc)
    else:  # pragma: no cover - the guard is the point
        raise AssertionError("an unregistered repository must not become a reference")
    finally:
        conn.close()
