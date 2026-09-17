"""Every check ends in a short delta: what moved, what is newly blocked, what needs the user.

Every check used to either dump the whole board or say nothing, which is how the
supervision gap in #72 stayed invisible for a day. One renderer, one record of the
last check, and every surface calls it last.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from papaya_agent_runtime import board, digest, owed, watch
from papaya_agent_runtime.cli import main
from papaya_agent_runtime.state import init_db, store

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


@pytest.fixture
def home(ppy_home, monkeypatch):
    monkeypatch.setenv("PPY_QUIET_INVITE", "1")
    return ppy_home


def _worker(conn, status: str) -> int:
    run_id = store.create_run(conn, "ship it")
    task_id = store.add_task(conn, run_id=run_id, title="build")
    store.set_task_status(conn, task_id, status)
    return task_id


def test_the_first_check_has_nothing_to_compare_and_says_so(home) -> None:
    conn = init_db()
    assert digest.check(conn, now=NOW) == ["first check, nothing to compare with yet"]
    assert digest.load()["at"] == NOW.isoformat(timespec="seconds")


def test_no_change_is_one_line_and_a_move_is_named(home) -> None:
    conn = init_db()
    task_id = _worker(conn, "in_progress")
    digest.check(conn, now=NOW)

    assert digest.check(conn, now=NOW + timedelta(minutes=5)) == ["no change since 12:00 UTC"]

    store.set_task_status(conn, task_id, "worker_done")
    todo_id = board.add("review it", task_id=task_id, conn=conn)
    lines = digest.check(conn, now=NOW + timedelta(minutes=10))

    assert lines == [
        f"since 12:05 UTC: moved: t{task_id} in_progress→worker_done; todo #{todo_id} added"
    ]


def test_newly_blocked_and_needs_you_are_their_own_lines(home, monkeypatch) -> None:
    conn = init_db()
    task_id = _worker(conn, "in_progress")
    other = _worker(conn, "in_progress")
    digest.check(conn, now=NOW)

    store.set_task_status(conn, task_id, "blocked")
    todo_id = board.add("decide the API shape", blocked_on="user", conn=conn)
    # A worker owed past the grace with nobody taking it up needs the user too.
    store.set_task_status(conn, other, "failed")
    stamp = (NOW - timedelta(minutes=30)).isoformat()
    conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (stamp, other))
    conn.commit()

    lines = digest.check(conn, now=NOW + timedelta(minutes=5))

    assert lines[0].startswith("since 12:00 UTC: moved: ")
    assert (
        f"t{task_id} in_progress→blocked" in lines[0] and f"t{other} in_progress→failed" in lines[0]
    )
    assert lines[1] == f"newly blocked: t{task_id} blocked; t{other} failed"
    assert lines[2].startswith(f"needs you: todo #{todo_id}: decide the API shape; t{other} failed")
    # Standing needs are said again with no change, so they are never lost to silence.
    again = digest.check(conn, now=NOW + timedelta(minutes=10))
    assert again[0].startswith("no change since 12:05 UTC; still needs you: ")


def test_pull_requests_move_when_the_surface_read_the_forge_and_carry_over_when_not(home) -> None:
    conn = init_db()
    digest.check(conn, now=NOW, prs={"PR #7": {"state": "OPEN", "ci": "pending"}})

    # A status check reads no forge: the pull requests are as the heartbeat saw them.
    assert digest.check(conn, now=NOW + timedelta(minutes=1)) == ["no change since 12:00 UTC"]
    assert digest.load()["prs"] == {"PR #7": {"state": "OPEN", "ci": "pending", "mergeable": None}}

    lines = digest.check(
        conn, now=NOW + timedelta(minutes=2), prs={"PR #7": {"state": "OPEN", "ci": "pass"}}
    )
    assert lines == ["since 12:01 UTC: moved: PR #7 ci pending→pass"]


def test_more_than_a_few_items_are_counted_not_listed(home) -> None:
    found = digest.Delta(since=NOW.isoformat(), moved=[f"t{i} new→in_progress" for i in range(7)])
    [line] = digest.render(found)
    assert line.endswith("t3 new→in_progress; and 3 more")


def test_the_heartbeat_tick_ends_in_the_delta(home) -> None:
    conn = init_db()
    _worker(conn, "in_progress")
    first = watch.render(watch.tick(conn, now=NOW))
    second = watch.render(watch.tick(conn, now=NOW + timedelta(minutes=5)))

    assert first.endswith(" || first check, nothing to compare with yet")
    assert second.endswith(" || no change since 12:00 UTC")


def test_status_and_run_end_in_the_delta(home, capsys, monkeypatch) -> None:
    conn = init_db()
    task_id = _worker(conn, "in_progress")
    monkeypatch.setattr(owed, "watch_running", lambda: True)

    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "delta: first check, nothing to compare with yet" in out

    store.set_task_status(conn, task_id, "worker_done")
    assert main(["status", "--team"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out[-1].startswith("delta: since ")
    assert f"moved: t{task_id} in_progress→worker_done" in out[-1]

    # `--json` is for a script: nothing is appended to it.
    assert main(["status", "--team", "--json"]) == 0
    assert "delta:" not in capsys.readouterr().out
