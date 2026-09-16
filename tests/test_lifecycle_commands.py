"""Lifecycle bookkeeping the manager used to do with Python one-liners.

`ppy task close`, `ppy task set-status`, `ppy lease release`, and
`ppy deliver --merged` exist so that closing out a task, repairing a status,
freeing a stuck slot, and recording a PR merged on GitHub each leave an event
behind instead of a silent UPDATE against `.ppy/state.db`.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from conftest import wait_until
from papaya_agent_runtime import lifecycle, repos
from papaya_agent_runtime.cli import main
from papaya_agent_runtime.delivery import DeliveryError, record_merged
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


def _finished_task(client, repo_name, title="ship"):
    task_id = client.dispatch_task(repo=repo_name, title=title)["task_id"]
    wait_until(
        lambda: client.task_status(task_id)["task"]["status"] == "worker_done",
        20,
        what=f"task {task_id} to finish",
    )
    return task_id


def _events(task_id, kind):
    conn = init_db()
    rows = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id", (task_id, kind)
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def _status(task_id):
    return store.get_task(init_db(), task_id)["status"]


# --------------------------------------------------------------------------- #
# ppy task close
# --------------------------------------------------------------------------- #


def test_close_sets_closed_records_the_reason_and_releases_the_lease(server, source_repo):
    srv, client = server
    added = repos.add_repo(source_repo)
    task_id = _finished_task(client, added.name)
    worktree = store.get_task(init_db(), task_id)["worktree_path"]
    assert Path(worktree).exists()

    assert main(["task", "close", str(task_id), "--reason", "superseded by task 91"]) == 0

    assert _status(task_id) == "closed"
    closed = _events(task_id, "task_closed")
    assert closed and closed[-1]["reason"] == "superseded by task 91"
    assert closed[-1]["previous_status"] == "worker_done"
    assert closed[-1]["lease_released"] is True
    assert _events(task_id, "lease_released")
    assert not Path(worktree).exists(), "closing a task must hand its worktree slot back"
    conn = init_db()
    lease = conn.execute("SELECT status FROM leases WHERE task_id = ?", (task_id,)).fetchone()
    assert lease["status"] == "released"


def test_close_refuses_without_a_reason(server, source_repo):
    srv, client = server
    added = repos.add_repo(source_repo)
    task_id = _finished_task(client, added.name)
    with pytest.raises(lifecycle.LifecycleError):
        lifecycle.close_task(task_id, "   ")
    assert _status(task_id) == "worker_done"


def test_closing_a_task_keeps_its_branch(server, source_repo):
    """A released slot must never destroy commits that were never pushed."""
    srv, client = server
    added = repos.add_repo(source_repo)
    task_id = _finished_task(client, added.name)
    task = store.get_task(init_db(), task_id)
    repo_path = store.get_repo(init_db(), added.name)["local_path"]

    lifecycle.close_task(task_id, "user changed direction")

    branches = subprocess.run(
        ["git", "-C", repo_path, "branch", "--list", task["branch"]],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert task["branch"] in branches


# --------------------------------------------------------------------------- #
# ppy task set-status
# --------------------------------------------------------------------------- #


def test_set_status_records_the_transition_and_the_note(server, source_repo):
    srv, client = server
    added = repos.add_repo(source_repo)
    task_id = _finished_task(client, added.name)

    rc = main(
        [
            "task",
            "set-status",
            str(task_id),
            "in_progress",
            "--note",
            "runner was alive; status was stale",
        ]
    )
    assert rc == 0
    assert _status(task_id) == "in_progress"
    recorded = _events(task_id, "status_set")
    assert recorded[-1] == {
        "task_id": task_id,
        "from": "worker_done",
        "to": "in_progress",
        "note": "runner was alive; status was stale",
    }


def test_set_status_rejects_a_status_no_reader_understands(server, source_repo):
    srv, client = server
    added = repos.add_repo(source_repo)
    task_id = _finished_task(client, added.name)
    with pytest.raises(lifecycle.LifecycleError) as exc:
        lifecycle.set_task_status(task_id, "done-ish")
    assert "allowed" in str(exc.value)
    assert _status(task_id) == "worker_done"
    # argparse refuses it before it ever reaches the store.
    with pytest.raises(SystemExit):
        main(["task", "set-status", str(task_id), "done-ish"])


def test_task_id_shorthand_still_snapshots(server, source_repo, capsys):
    srv, client = server
    added = repos.add_repo(source_repo)
    task_id = _finished_task(client, added.name)
    assert main(["task", str(task_id)]) == 0
    assert f"task {task_id}" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# ppy lease release
# --------------------------------------------------------------------------- #


def test_lease_release_frees_the_slot_with_an_event(server, source_repo):
    srv, client = server
    added = repos.add_repo(source_repo)
    task_id = _finished_task(client, added.name)
    worktree = store.get_task(init_db(), task_id)["worktree_path"]

    assert main(["lease", "release", str(task_id), "--reason", "worker died, slot stuck"]) == 0

    assert not Path(worktree).exists()
    released = _events(task_id, "lease_released")
    assert released[-1]["reason"] == "worker died, slot stuck"
    assert released[-1]["removed_branch"] is False
    # The task itself is untouched: releasing a slot is not a verdict on the work.
    assert _status(task_id) == "worker_done"


def test_lease_release_is_idempotent(server, source_repo):
    srv, client = server
    added = repos.add_repo(source_repo)
    task_id = _finished_task(client, added.name)
    lifecycle.release_task_lease(task_id)
    again = lifecycle.release_task_lease(task_id)
    assert again["released"] is False
    assert "already released" in again["note"]


def test_lease_release_on_a_task_with_no_lease_says_so(ppy_home):
    conn = init_db()
    run_id = store.create_run(conn, "objective")
    task_id = store.add_task(conn, run_id=run_id, title="never dispatched")
    with pytest.raises(lifecycle.LifecycleError) as exc:
        lifecycle.release_task_lease(task_id)
    assert "no lease on record" in str(exc.value)


# --------------------------------------------------------------------------- #
# ppy deliver --merged
# --------------------------------------------------------------------------- #


def test_deliver_merged_records_delivery_without_pushing(server, source_repo):
    srv, client = server
    added = repos.add_repo(source_repo)
    task_id = _finished_task(client, added.name)
    branch = store.get_task(init_db(), task_id)["branch"]
    sha = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"

    def _remote_tip() -> str:
        return subprocess.run(
            ["git", "-C", source_repo, "rev-parse", branch],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    before = _remote_tip()  # the worker pushed its own branch, as a finished worker does

    assert main(["deliver", str(task_id), "--merged", sha]) == 0

    assert _status(task_id) == "delivered"
    delivered = _events(task_id, "delivered")
    assert delivered[-1]["head_sha"] == sha
    assert delivered[-1]["merged_outside_mm"] is True
    assert delivered[-1]["pr_url"] is None

    # Nothing left the machine: recording a merge moves no ref on the origin.
    assert _remote_tip() == before


def test_deliver_merged_needs_a_real_sha(server, source_repo):
    srv, client = server
    added = repos.add_repo(source_repo)
    task_id = _finished_task(client, added.name)
    with pytest.raises(DeliveryError):
        record_merged(task_id, "the-one-i-merged")
    assert _status(task_id) == "worker_done"


def test_deliver_merged_does_not_need_an_approved_review(server, source_repo):
    """The commit already shipped; the exact-HEAD gate has nothing left to guard."""
    srv, client = server
    added = repos.add_repo(source_repo)
    task_id = _finished_task(client, added.name)
    res = record_merged(task_id, "0123456789abcdef0123456789abcdef01234567")
    assert res.pushed is False
    assert res.pr_url is None
    assert _status(task_id) == "delivered"
    # The landing goes on the task itself, so the heartbeat stops asking the
    # forge about a branch whose merge is already known.
    task = store.get_task(init_db(), task_id)
    assert task["merged_sha"] == "0123456789abcdef0123456789abcdef01234567"
    assert task["merged_at"]
