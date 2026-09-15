"""A resumable task whose worktree is gone gets one back, instead of crashing (issue #58).

On 2026-09-06 four Claude workers were cut off by a usage limit and marked
failed. Task 158 had no commits, so its pristine lease was handed back; a prune
then took the slot, and `ppy resume 158` crashed twice with FileNotFoundError
before the task had to be closed and re-dispatched, losing the session.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from conftest import scale
from papaya_agent_runtime import lifecycle, repos
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor.core import Supervisor


def _wait_for(predicate, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + scale(timeout)
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("timed out waiting")


def _status(task_id: int) -> str:
    return store.get_task(init_db(), task_id)["status"]


def _events(task_id: int, kind: str) -> list[dict]:
    rows = (
        init_db()
        .execute(
            "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id",
            (task_id, kind),
        )
        .fetchall()
    )
    return [json.loads(r["payload"]) for r in rows]


def test_a_failed_task_whose_pristine_lease_was_released_resumes_into_a_new_worktree(
    ppy_home, source_repo
) -> None:
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    task = supervisor.dispatch_task(
        repo=added.name, title="cut off early", instructions="FAIL", provider="fake"
    )
    task_id = task["task_id"]
    _wait_for(lambda: _status(task_id) == "failed")
    _wait_for(lambda: not Path(task["worktree_path"]).exists())
    before = store.get_task(init_db(), task_id)
    assert before["worktree_path"] == task["worktree_path"]  # the record still names it

    resumed = supervisor.resume_task(task_id)
    rebuilt = resumed["worktree_rebuilt"]
    assert rebuilt is not None
    assert rebuilt["previous_worktree"] == task["worktree_path"]
    assert rebuilt["worktree_path"] != task["worktree_path"]
    assert "base commit" in rebuilt["started_from"]
    _wait_for(lambda: _status(task_id) == "worker_done")

    after = store.get_task(init_db(), task_id)
    assert after["worktree_path"] == rebuilt["worktree_path"]
    assert Path(after["worktree_path"]).is_dir()
    assert after["branch"] == before["branch"]  # the task keeps its own branch name
    assert after["lease_id"] == rebuilt["lease"]
    assert _events(task_id, "worktree_rebuilt")[0]["started_from"] == rebuilt["started_from"]
    assert Path(after["worktree_path"], f"ppy-fake-{task_id}.txt").exists()


def test_a_task_with_pushed_work_is_rebuilt_from_its_branch(ppy_home, source_repo) -> None:
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    task = supervisor.dispatch_task(repo=added.name, title="pushed then lost", provider="fake")
    task_id = task["task_id"]
    _wait_for(lambda: _status(task_id) == "worker_done")
    row = store.get_task(init_db(), task_id)
    work = Path(row["worktree_path"], f"ppy-fake-{task_id}.txt")
    assert work.exists()

    # The worker was then judged failed and its slot taken, local branch and all.
    store.set_task_status(init_db(), task_id, "failed")
    supervisor.release_task_lease(task_id, remove_branch=True)
    assert not Path(row["worktree_path"]).exists()

    resumed = supervisor.resume_task(task_id, "one more line")
    rebuilt = resumed["worktree_rebuilt"]
    assert rebuilt is not None
    assert rebuilt["started_from"].startswith(f"branch {row['branch']} at ")
    _wait_for(lambda: _status(task_id) == "worker_done")

    after = store.get_task(init_db(), task_id)
    text = Path(after["worktree_path"], f"ppy-fake-{task_id}.txt").read_text()
    assert f"work for task {task_id}: pushed then lost" in text  # the pushed work came back
    assert "steer: one more line" in text  # and the resume ran on top of it


def test_resume_refuses_plainly_when_nothing_can_be_rebuilt(ppy_home, source_repo) -> None:
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    task = supervisor.dispatch_task(
        repo=added.name, title="orphan", instructions="FAIL", provider="fake"
    )
    task_id = task["task_id"]
    _wait_for(lambda: _status(task_id) == "failed")
    _wait_for(lambda: not Path(task["worktree_path"]).exists())
    conn = init_db()
    conn.execute("UPDATE tasks SET repo_id = NULL WHERE id = ?", (task_id,))
    conn.commit()
    try:
        supervisor.resume_task(task_id)
    except Exception as exc:  # noqa: BLE001
        assert "no registered repository to rebuild one from" in str(exc)
    else:
        raise AssertionError("resume should have refused")
    assert _status(task_id) == "failed"


def test_lifecycle_release_then_resume_round_trip(ppy_home, source_repo) -> None:
    """The by-hand path: `ppy task release-lease` then `ppy resume`."""
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    task = supervisor.dispatch_task(repo=added.name, title="by hand", provider="fake")
    task_id = task["task_id"]
    _wait_for(lambda: _status(task_id) == "worker_done")
    lifecycle.release_task_lease(task_id, reason="test")
    supervisor._leases.pop(task_id, None)
    assert not Path(task["worktree_path"]).exists()

    resumed = supervisor.resume_task(task_id, "again")
    assert resumed["worktree_rebuilt"] is not None
    _wait_for(lambda: _status(task_id) == "worker_done")
