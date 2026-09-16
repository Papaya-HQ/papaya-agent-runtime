"""Hermetic tests for the exact-HEAD review gate and gated delivery."""

from __future__ import annotations

import subprocess
import time

import pytest

from conftest import wait_until
from papaya_agent_runtime import repos
from papaya_agent_runtime.delivery import DeliveryError, deliver
from papaya_agent_runtime.review import build_bundle, is_approved_at_head, record_review
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


def _wait_status(client, task_id, want, timeout=15.0):
    wait_until(
        lambda: client.task_status(task_id)["task"]["status"] == want,
        timeout,
        what=f"task {task_id} to reach {want}",
        interval=0.1,
    )


def _completed_task(client, source_repo) -> dict:
    added = repos.add_repo(source_repo)
    resp = client.dispatch_task(repo=added.name, title="do work", instructions="")
    _wait_status(client, resp["task_id"], "worker_done")
    return resp


def test_review_bundle_and_gate(server, source_repo) -> None:
    _srv, client = server
    resp = _completed_task(client, source_repo)
    task_id = resp["task_id"]

    bundle = build_bundle(task_id)
    assert bundle.files_changed >= 1
    assert bundle.base_sha != bundle.head_sha

    approved, _ = is_approved_at_head(task_id)
    assert approved is False  # no review yet

    record_review(task_id, "approved", "looks good")
    approved, _ = is_approved_at_head(task_id)
    assert approved is True


def test_gate_reopens_when_head_moves(server, source_repo) -> None:
    _srv, client = server
    resp = _completed_task(client, source_repo)
    task_id = resp["task_id"]
    record_review(task_id, "approved")
    assert is_approved_at_head(task_id)[0] is True

    # A new commit after approval invalidates the review.
    worktree = client.task_status(task_id)["task"]["worktree_path"]
    with open(f"{worktree}/extra.txt", "w", encoding="utf-8") as fh:
        fh.write("more\n")
    subprocess.run(["git", "add", "-A"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "extra"],
        cwd=worktree,
        check=True,
    )
    approved, reason = is_approved_at_head(task_id)
    assert approved is False
    assert "re-review" in reason


def test_delivery_refuses_without_approval(server, source_repo) -> None:
    _srv, client = server
    resp = _completed_task(client, source_repo)
    with pytest.raises(DeliveryError):
        deliver(resp["task_id"], push=False, open_pr=False)


def test_delivery_pushes_after_approval(server, source_repo) -> None:
    _srv, client = server
    resp = _completed_task(client, source_repo)
    task_id = resp["task_id"]
    record_review(task_id, "approved")

    result = deliver(task_id, push=True, open_pr=False)
    assert result.pushed is True

    # The lease branch now exists in the source repo.
    branches = subprocess.run(
        ["git", "branch", "--list", result.branch],
        cwd=source_repo,
        capture_output=True,
        text=True,
    ).stdout
    assert result.branch in branches
    assert client.task_status(task_id)["task"]["status"] == "delivered"
