"""Two tasks in one repository editing the same files are named at dispatch (issue #61)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from conftest import make_git_repo, wait_until
from papaya_agent_runtime import overlap, repos
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor.client import SupervisorClient
from papaya_agent_runtime.supervisor.server import SupervisorServer


@pytest.fixture
def server(ppy_home):
    srv = SupervisorServer()
    srv.start_background()
    client = SupervisorClient(srv.socket_path)
    for _ in range(50):
        try:
            if client.ping().get("ok"):
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.05)
    yield srv, client
    srv.stop()


def _wait_terminal(client, task_id, timeout=15.0):
    def finished():
        status = client.task_status(task_id)["task"]["status"]
        return status if status in {"worker_done", "blocked", "failed"} else None

    return wait_until(finished, timeout, what=f"task {task_id} to finish", interval=0.1)


# --------------------------------------------------------------------------- #
# What a brief says it touches
# --------------------------------------------------------------------------- #


def test_touched_paths_reads_the_touches_line_and_real_backtick_spans(tmp_path) -> None:
    root = tmp_path / "repo"
    (root / "src" / "radar").mkdir(parents=True)
    (root / "src" / "radar" / "compose.py").write_text("x = 1\n")
    brief = """# Split the composer

Touches: src/radar/compose.py, src/radar/cards/

Run `uv run pytest -q` and edit `src/radar/compose.py`; `RADAR_VERSION` is a name,
`docs/nothing-here.md` does not exist, and `--flag` is a flag.
"""
    assert overlap.touched_paths(brief, str(root)) == ["src/radar/compose.py", "src/radar/cards"]
    # Without a repo to check against, only the Touches line is trusted.
    assert overlap.touched_paths(brief, None) == ["src/radar/compose.py", "src/radar/cards"]
    assert overlap.touched_paths("no paths in here", str(root)) == []


# --------------------------------------------------------------------------- #
# In flight means in flight (issue #56)
# --------------------------------------------------------------------------- #


def _task_row(conn, repo_id, *, status: str, worktree: str | None, age: timedelta):
    run_id = store.create_run(conn, "r")
    task_id = store.add_task(conn, run_id=run_id, title=f"t{status}", repo_id=repo_id)
    store.update_task_fields(conn, task_id, worktree_path=worktree, branch=f"ppy/task-{task_id}")
    store.set_task_status(conn, task_id, status)
    touched = (datetime.now(UTC) - age).isoformat()
    conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (touched, task_id))
    conn.commit()
    return store.get_task(conn, task_id)


def test_in_flight_needs_a_live_status_a_worktree_on_disk_and_recent_activity(
    ppy_home, tmp_path
) -> None:
    conn = init_db()
    repo_id = store.add_repo(
        conn,
        name="r",
        origin="o",
        local_path=str(tmp_path / "c"),
        default_branch="main",
        base_sha=None,
    )
    live = str(tmp_path / "wt")
    Path(live).mkdir()
    fresh = timedelta(hours=1)
    assert overlap.is_in_flight(
        _task_row(conn, repo_id, status="in_progress", worktree=live, age=fresh)
    )
    assert overlap.is_in_flight(
        _task_row(conn, repo_id, status="worker_done", worktree=live, age=fresh)
    )
    # A month-old row that nobody closed, worktree or not, is not in flight.
    assert not overlap.is_in_flight(
        _task_row(conn, repo_id, status="in_progress", worktree=live, age=timedelta(days=30))
    )
    # No lease worktree: nothing is being written anywhere.
    assert not overlap.is_in_flight(
        _task_row(conn, repo_id, status="in_progress", worktree=str(tmp_path / "gone"), age=fresh)
    )
    assert not overlap.is_in_flight(
        _task_row(conn, repo_id, status="delivered", worktree=live, age=fresh)
    )


# --------------------------------------------------------------------------- #
# The advisory at dispatch
# --------------------------------------------------------------------------- #


@pytest.fixture
def module_repo(tmp_path):
    path = tmp_path / "source"
    make_git_repo(path)
    (path / "src" / "radar").mkdir(parents=True)
    (path / "src" / "radar" / "compose.py").write_text("x = 1\n")
    (path / "src" / "radar" / "cards.py").write_text("y = 1\n")
    import subprocess

    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "module"], check=True)
    return str(path)


def test_dispatch_names_the_in_flight_task_touching_the_same_module(server, module_repo) -> None:
    srv, client = server
    added = repos.add_repo(module_repo)
    first = client.dispatch_task(
        repo=added.name,
        title="split the composer",
        instructions="# Split\n\nTouches: src/radar/compose.py\n",
    )
    assert first["ok"], first
    assert first["overlap_advisory"] is None
    _wait_terminal(client, first["task_id"])

    # Named in a backtick span this time, and the file exists in the repo.
    second = client.dispatch_task(
        repo=added.name,
        title="add a card",
        instructions="# Card\n\nEdit `src/radar/compose.py` to register the card.\n",
    )
    assert second["ok"], second
    advisory = second["overlap_advisory"]
    assert advisory, "the second dispatch should have been told about the first"
    assert f'task {first["task_id"]} "split the composer"' in advisory
    assert "src/radar/compose.py" in advisory
    assert f"--stack-on {first['task_id']}" in advisory
    _wait_terminal(client, second["task_id"])

    # A different file in the same tree: nothing to say.
    third = client.dispatch_task(
        repo=added.name, title="cards only", instructions="Touches: src/radar/cards.py\n"
    )
    assert third["ok"], third
    assert third["overlap_advisory"] is None
    _wait_terminal(client, third["task_id"])

    # A module directory covers the files inside it; the task being stacked on
    # is not reported, the others are.
    fourth = client.dispatch_task(
        repo=added.name,
        title="rework the module",
        instructions="Touches: src/radar/\n",
        stack_on=first["task_id"],
    )
    assert fourth["ok"], fourth
    advisory = fourth["overlap_advisory"]
    assert advisory
    assert f"--stack-on {first['task_id']}" not in advisory
    assert f"--stack-on {second['task_id']}" in advisory
    assert f"--stack-on {third['task_id']}" in advisory
