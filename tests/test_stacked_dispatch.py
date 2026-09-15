"""Stacked work starts from the exact branch it builds on, and delivers against it."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from papaya_agent_runtime import delivery, repos
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.state.db import _column_names
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


def _git(path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _wait_terminal(client, task_id, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.task_status(task_id)["task"]["status"]
        if status in {"worker_done", "blocked", "failed"}:
            return status
        time.sleep(0.1)
    raise AssertionError("task never finished")


_MIGRATION = '''"""a revision"""

revision = "{revision}"
down_revision = "base"


def upgrade() -> None:
    pass
'''


def _commit_file(worktree: str, path: str, body: str) -> None:
    target = Path(worktree) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    _git(worktree, "add", path)
    _git(worktree, "commit", "-qm", f"add {path}")


def test_schema_gains_stacked_on_on_fresh_and_existing_databases(ppy_home) -> None:
    conn = init_db()
    assert "stacked_on" in _column_names(conn, "tasks")
    # An older database without the column is migrated forward, idempotently.
    conn.execute("ALTER TABLE tasks DROP COLUMN stacked_on")
    conn.commit()
    assert "stacked_on" not in _column_names(conn, "tasks")
    conn = init_db()
    assert "stacked_on" in _column_names(conn, "tasks")
    init_db()  # a second run must not fail on the now-present column


def test_dispatch_with_base_starts_from_that_branch_and_records_it(server, source_repo) -> None:
    srv, client = server
    # A branch ahead of main in the origin the registered clone will fetch from.
    _git(source_repo, "switch", "-q", "-c", "stack/base")
    (Path(source_repo) / "stacked.txt").write_text("stacked work\n")
    _git(source_repo, "add", "stacked.txt")
    _git(source_repo, "commit", "-qm", "stacked base commit")
    stacked_tip = _git(source_repo, "rev-parse", "HEAD")
    _git(source_repo, "switch", "-q", "main")

    added = repos.add_repo(source_repo)
    resp = client.dispatch_task(repo=added.name, title="on the stack", base="stack/base")
    assert resp["ok"], resp
    task_id = resp["task_id"]
    _wait_terminal(client, task_id)

    task = client.task_status(task_id)["task"]
    assert task["base_sha"] == stacked_tip
    assert task["stacked_on"] == "stack/base"
    # The fake worker commits on top, so the tip is the worktree's ancestor, not its HEAD.
    subprocess.run(
        ["git", "-C", task["worktree_path"], "merge-base", "--is-ancestor", stacked_tip, "HEAD"],
        check=True,
    )
    assert _git(task["worktree_path"], "merge-base", "--is-ancestor", "main", stacked_tip) == ""
    # The worker's own branch still exists at that tip, ready for `ppy deliver`.
    assert _git(task["worktree_path"], "rev-parse", "--abbrev-ref", "HEAD") == task["branch"]


def test_dispatch_with_an_unknown_base_fails_plainly_and_frees_the_slot(server, source_repo):
    srv, client = server
    added = repos.add_repo(source_repo)
    resp = client.dispatch_task(repo=added.name, title="bad base", base="does/not-exist")
    assert not resp.get("ok")
    assert "cannot start from 'does/not-exist'" in resp["error"]
    conn = init_db()
    failed = conn.execute("SELECT status FROM tasks ORDER BY id DESC LIMIT 1").fetchone()
    assert failed["status"] == "failed"


def test_dispatch_warns_when_work_in_flight_already_adds_a_migration(server, source_repo) -> None:
    """Two migrations off one head leave two Alembic heads; say so before the second starts."""
    srv, client = server
    added = repos.add_repo(source_repo)
    first = client.dispatch_task(repo=added.name, title="add the events table")
    assert first["ok"], first
    assert first["migration_advisory"] is None  # nothing else is in flight yet
    _wait_terminal(client, first["task_id"])
    ahead = client.task_status(first["task_id"])["task"]
    _commit_file(
        ahead["worktree_path"],
        "backend/alembic/versions/aaa.py",
        _MIGRATION.format(revision="aaa"),
    )

    second = client.dispatch_task(repo=added.name, title="add the sessions table")
    assert second["ok"], second
    advisory = second["migration_advisory"]
    assert advisory, "the second dispatch should have been told about the first"
    assert f'task {first["task_id"]} "add the events table"' in advisory
    assert ahead["branch"] in advisory
    assert "backend/alembic/versions/aaa.py" in advisory
    assert f"--stack-on {first['task_id']}" in advisory
    # An advisory, not a refusal: the second task was dispatched all the same.
    assert client.task_status(second["task_id"])["task"]["id"] == second["task_id"]


def test_dispatch_stays_quiet_when_the_work_in_flight_adds_no_migration(server, source_repo):
    srv, client = server
    added = repos.add_repo(source_repo)
    first = client.dispatch_task(repo=added.name, title="rename the events model")
    _wait_terminal(client, first["task_id"])
    ahead = client.task_status(first["task_id"])["task"]
    _commit_file(ahead["worktree_path"], "backend/models/events.py", "EVENTS = 1\n")

    second = client.dispatch_task(repo=added.name, title="add the sessions table")
    assert second["ok"], second
    assert second["migration_advisory"] is None


def test_deliver_defaults_the_pr_base_to_the_recorded_stack(ppy_home, monkeypatch) -> None:
    conn = init_db()
    run_id = store.create_run(conn, "stacked")
    task_id = store.add_task(conn, run_id=run_id, title="t")
    store.update_task_fields(
        conn, task_id, worktree_path="/tmp/wt", branch="ppy/task-9-abc", stacked_on="feature/base"
    )
    calls: list[list[str]] = []

    def fake_run(argv, cwd=None):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="https://example/pr/1\n", stderr="")

    monkeypatch.setattr(delivery, "_run", fake_run)
    monkeypatch.setattr(delivery, "is_approved_at_head", lambda tid: (True, ""))
    monkeypatch.setattr(delivery, "head_sha", lambda wt: "f" * 40)
    monkeypatch.setattr(delivery, "_pr_tool", lambda: "gh")

    res = delivery.deliver(task_id)
    pr_argv = next(a for a in calls if a[:3] == ["gh", "pr", "create"])
    assert pr_argv[pr_argv.index("--base") + 1] == "feature/base"
    assert res.pr_url == "https://example/pr/1"

    # An explicit base still wins.
    calls.clear()
    delivery.deliver(task_id, base="main")
    pr_argv = next(a for a in calls if a[:3] == ["gh", "pr", "create"])
    assert pr_argv[pr_argv.index("--base") + 1] == "main"
