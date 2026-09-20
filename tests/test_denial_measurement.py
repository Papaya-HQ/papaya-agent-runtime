"""Denials are countable by kind and by shape, so a change to the rules is checkable.

The brief's last goal: "the most common rule-shape violations get the exact
replacement in the refusal, and their rate is measurable". A count with no window
cannot answer "did last week's change work" — the all-time tally moves too slowly
to show anything. So the tally takes a window, and command-shape denials are
bucketed by the rewrite row they match, which is the same bucket the steer now
tells the worker about.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from papaya_agent_runtime import team, tool_learning
from papaya_agent_runtime.state import init_db, store

WT = "/wt"


@pytest.fixture
def repo(ppy_home) -> int:
    conn = init_db()
    run_id = store.create_run(conn, "run")
    repo_id = store.add_repo(
        conn,
        name="api",
        origin="https://github.com/acme/api",
        local_path=WT,
        default_branch="main",
        base_sha=None,
    )
    task_id = store.add_task(conn, run_id=run_id, title="w", repo_id=repo_id)
    conn.commit()
    conn.close()
    return task_id


def _denial(task_id: int, command: str, *, days_ago: float = 0.0, worktree: str = WT) -> None:
    verdict = tool_learning.classify("Bash", command, worktree)
    conn = init_db()
    store.append_event(
        conn,
        kind=tool_learning.PERMISSION_DENIED,
        payload={
            "tool": "Bash",
            "command": command,
            "kind": verdict.kind,
            "program": verdict.program,
            "pattern": verdict.pattern,
            "in_family": verdict.in_family,
            "reason": verdict.reason,
            "worktree": worktree,
        },
        task_id=task_id,
    )
    if days_ago:
        at = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
        conn.execute(
            "UPDATE events SET created_at = ? WHERE id = (SELECT MAX(id) FROM events)", (at,)
        )
    conn.commit()
    conn.close()


def test_denials_are_counted_by_kind_within_a_window(repo) -> None:
    _denial(repo, "terraform plan")
    _denial(repo, f"cd {WT} && git status")
    _denial(repo, "sudo ls /root")
    _denial(repo, "terraform apply", days_ago=30)

    assert tool_learning.counts(7)["api"] == {
        tool_learning.PROFILE_GAP: 1,
        tool_learning.COMMAND_SHAPE: 1,
        tool_learning.POLICY_REFUSAL: 1,
    }
    # Without a window, the old all-time answer is unchanged.
    assert tool_learning.counts()["api"][tool_learning.PROFILE_GAP] == 2


def test_command_shape_denials_are_counted_by_the_shape_they_used(tmp_path, repo) -> None:
    """Bucketed by the rewrite that MATCHED, which is the one the worker was sent.

    It used to take the first of several generic rows, so a `cd <subdir> && git …`
    counted as a plain `cd` and hid which rewrite was not landing.
    """
    (tmp_path / "backend").mkdir()
    wt = str(tmp_path)
    _denial(repo, f"cd {wt} && git status --short", worktree=wt)
    _denial(repo, f"cd {wt} && ruff check .", worktree=wt)
    _denial(repo, f"cd {wt}/backend && git add -A", worktree=wt)
    _denial(repo, "cat notes.md | head -200", worktree=wt)
    _denial(repo, "terraform plan", worktree=wt)  # not a shape denial at all

    shapes = tool_learning.shape_counts(7)["api"]

    assert shapes["cd <your worktree> && <command>"] == 2
    assert shapes["cd <worktree>/<subdir> && git <args>"] == 1
    assert shapes["<command> | head -<n>, | tail -<n>, | less"] == 1
    assert sum(shapes.values()) == 4


def test_an_old_shape_denial_is_outside_the_window(repo) -> None:
    """The point of the window: last month cannot hide this week's improvement."""
    _denial(repo, f"cd {WT} && git status", days_ago=30)

    assert tool_learning.shape_counts(7) == {}
    assert tool_learning.shape_counts(60)["api"]


def test_the_workers_view_shows_the_tally_by_kind_and_shape(repo) -> None:
    _denial(repo, f"cd {WT} && git status --short")
    _denial(repo, "terraform plan")

    lines = team.denial_lines()

    assert any(line.startswith("api: 2 (") for line in lines)
    assert any("command_shape 1" in line for line in lines)
    assert any("shape x1: cd <your worktree> && <command>" in line for line in lines)


def test_the_json_view_carries_the_window_the_counts_came_from(repo) -> None:
    _denial(repo, "terraform plan")

    found = team.workers_json([])["denials"]

    assert found["days"] == team.DENIAL_WINDOW_DAYS
    assert found["kinds"]["api"] == {tool_learning.PROFILE_GAP: 1}
    assert found["shapes"] == {}


def test_a_hook_refusal_is_its_own_row_in_the_tally(repo) -> None:
    """The whole point: hook refusals stop being counted as profile gaps."""
    conn = init_db()
    store.append_event(
        conn,
        kind=tool_learning.PERMISSION_DENIED,
        payload={
            "tool": "Bash",
            "command": "git push origin HEAD:ppy/task-1-abc",
            "kind": tool_learning.HOOK_REFUSAL,
            "program": "git",
            "hook": ".claude/hooks/verify-before-push.sh",
            "reason": "the repository's own hook refused it",
            "worktree": WT,
        },
        task_id=repo,
    )
    conn.commit()
    conn.close()

    assert tool_learning.counts(7)["api"] == {tool_learning.HOOK_REFUSAL: 1}


def test_counting_never_raises_on_an_unreadable_ledger(monkeypatch, repo) -> None:
    monkeypatch.setattr("papaya_agent_runtime.state.init_db", lambda *a, **kw: 1 / 0, raising=False)
    assert isinstance(tool_learning.counts(7), dict)
    assert isinstance(tool_learning.shape_counts(7), dict)


def test_the_denial_tally_is_served_by_an_index_not_a_full_scan(repo) -> None:
    """`ppy workers` runs this on every paint, and under `--follow` on every tick.

    Two full scans of a table that only grows is a cost that shows up exactly when
    somebody is watching workers most closely.
    """
    _denial(repo, "terraform plan")

    conn = init_db()
    try:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT e.payload, r.name FROM events e "
            "LEFT JOIN tasks t ON t.id = e.task_id LEFT JOIN repos r ON r.id = t.repo_id "
            "WHERE e.kind = ? AND e.created_at >= ?",
            (tool_learning.PERMISSION_DENIED, "2026-01-01"),
        ).fetchall()
    finally:
        conn.close()

    said = " ".join(str(row[-1]) for row in plan)
    assert "idx_events_kind_created" in said, said
    assert "SCAN e" not in said, said
