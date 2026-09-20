"""One predicate decides who pushes, and everything asks it.

Two used to disagree. `environment.push_is_gated` — any readable `PreToolUse`
hook on `Bash`, even a logging one — decided what the WORKER was told; the push
itself and its timeout read `repos.push_hook_runs_full_suite`. A repository
matching the first and not the second told its worker "do not push, the runtime
pushes for you" and then had nothing gate-aware to push for it: the branch fell
through to the ungated rescue path, which asks no gate at all.
"""

from __future__ import annotations

import json

import pytest

from papaya_agent_runtime import environment, turn_end
from papaya_agent_runtime.state import init_db, store

SETTINGS = {
    "hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"command": "${CLAUDE_PROJECT_DIR}/.claude/log.sh"}]}
        ]
    }
}


@pytest.fixture
def repo(ppy_home, tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    conn = init_db()
    run_id = store.create_run(conn, "run")
    repo_id = store.add_repo(
        conn,
        name="api",
        origin="https://github.com/acme/api",
        local_path=str(worktree),
        default_branch="main",
        base_sha=None,
    )
    task_id = store.add_task(conn, run_id=run_id, title="w", repo_id=repo_id)
    conn.execute("UPDATE tasks SET worktree_path = ? WHERE id = ?", (str(worktree), task_id))
    conn.commit()
    conn.close()
    return {"worktree": worktree, "repo_id": repo_id, "task_id": task_id}


def _row(repo):
    conn = init_db()
    try:
        return conn.execute("SELECT * FROM repos WHERE id = ?", (repo["repo_id"],)).fetchone()
    finally:
        conn.close()


def _task(repo):
    conn = init_db()
    try:
        return store.get_task(conn, repo["task_id"])
    finally:
        conn.close()


def _register_hook(repo) -> None:
    (repo["worktree"] / ".claude").mkdir()
    (repo["worktree"] / ".claude" / "settings.json").write_text(
        json.dumps(SETTINGS), encoding="utf-8"
    )


def test_a_repository_with_a_hook_but_no_recorded_flag_is_gated_everywhere(repo) -> None:
    """The mismatch case. Both answers have to be the same one.

    The worker is told not to push, so the runtime must be the thing that pushes —
    with its gate check, not through the ungated rescue.
    """
    _register_hook(repo)

    conn = init_db()
    try:
        told_not_to_push = environment.push_is_gated(_row(repo), str(repo["worktree"]))
        runtime_pushes = turn_end.push_is_gated(conn, _task(repo))
        wait = turn_end.push_wait_seconds(conn, _task(repo))
    finally:
        conn.close()

    assert told_not_to_push is True
    assert runtime_pushes is True, "told not to push, and nothing gate-aware pushes for it"
    assert wait >= turn_end.GATED_PUSH_FLOOR_SECONDS


def test_a_repository_with_the_flag_but_no_readable_hook_is_gated_everywhere(repo) -> None:
    """The other direction: a recorded git pre-push hook the worktree cannot show."""
    conn = init_db()
    conn.execute("UPDATE repos SET push_hook_runs_full_suite = 1 WHERE id = ?", (repo["repo_id"],))
    conn.commit()
    conn.close()

    conn = init_db()
    try:
        assert environment.push_is_gated(_row(repo), str(repo["worktree"])) is True
        assert turn_end.push_is_gated(conn, _task(repo)) is True
        assert turn_end.push_wait_seconds(conn, _task(repo)) >= turn_end.GATED_PUSH_FLOOR_SECONDS
    finally:
        conn.close()


def test_a_repository_with_neither_is_ungated_everywhere(repo) -> None:
    conn = init_db()
    try:
        assert environment.push_is_gated(_row(repo), str(repo["worktree"])) is False
        assert turn_end.push_is_gated(conn, _task(repo)) is False
        assert turn_end.push_wait_seconds(conn, _task(repo)) == turn_end.PUSH_SECONDS
    finally:
        conn.close()


def test_what_the_worker_is_told_matches_who_pushes(repo) -> None:
    """The rules and the environment block used to contradict each other.

    The command rules said "Push with exactly `git push origin HEAD:<branch>`" while
    the environment block said "do not push". The worker followed the rules, and was
    refused — which is most of issue #83's fourteen occurrences.
    """
    _register_hook(repo)
    from papaya_agent_runtime.providers.command_rules import command_rules

    gated = environment.push_is_gated(_row(repo), str(repo["worktree"]))
    rules = command_rules("claude", "ppy/task-1-abc", runtime_pushes=gated)
    block = environment.render(
        environment.for_repo(_row(repo)),
        task_id=repo["task_id"],
        evidence_path="/wt/.ppy-evidence",
    )

    assert gated is True
    assert "git push origin HEAD:" not in rules
    assert "Do not push in this repository" in rules
    # And nothing in what the worker reads tells it to push after all.
    assert "Push with exactly" not in rules + block
