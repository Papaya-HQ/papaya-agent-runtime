"""Hermetic end-to-end test of the fake provider through the runner guardian."""

from __future__ import annotations

import os

from papaya_agent_runtime import repos
from papaya_agent_runtime.providers.base import TaskSpec
from papaya_agent_runtime.providers.fake import FakeProvider
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor.runner import RunnerGuardian
from papaya_agent_runtime.worktree import LeaseManager


def _dispatch_spec(source_repo, instructions=""):
    added = repos.add_repo(source_repo)
    conn = init_db()
    run_id = store.create_run(conn, "demo")
    repo_row = store.get_repo(conn, added.name)
    task_id = store.add_task(
        conn, run_id=run_id, title="do work", repo_id=repo_row["id"], provider="fake"
    )
    lease = LeaseManager(backend="git").acquire(
        repo_path=added.local_path, repo_id=repo_row["id"], task_id=task_id
    )
    spec = TaskSpec(
        task_id=task_id,
        title="do work",
        instructions=instructions,
        worktree_path=lease.worktree_path,
        base_sha=lease.base_sha or "",
        provider="fake",
        run_id=run_id,
    )
    return spec, run_id, task_id


def test_fake_worker_completes_and_commits(ppy_home, source_repo) -> None:
    spec, run_id, task_id = _dispatch_spec(source_repo)
    result = RunnerGuardian(FakeProvider()).run(spec)

    assert result.status == "completed"
    assert result.head_sha
    assert result.session_id
    assert os.path.exists(os.path.join(spec.worktree_path, f"ppy-fake-{task_id}.txt"))

    conn = init_db()
    assert store.get_task(conn, task_id)["status"] == "worker_done"
    assert store.usage_totals(conn, run_id)["input_tokens"] == 250
    # A durable session id was recorded.
    sess = conn.execute("SELECT * FROM sessions WHERE task_id = ?", (task_id,)).fetchone()
    assert sess["provider_session_id"] == result.session_id


def test_fake_worker_blocked_path(ppy_home, source_repo) -> None:
    spec, run_id, task_id = _dispatch_spec(source_repo, instructions="ASK: which API?")
    result = RunnerGuardian(FakeProvider()).run(spec)
    assert result.status == "blocked"
    assert "which API" in (result.question or "")
    conn = init_db()
    assert store.get_task(conn, task_id)["status"] == "blocked"
    kinds = [e["kind"] for e in store.actionable_events(conn, run_id)]
    assert "question" in kinds


def test_a_live_denial_is_recorded_while_the_worker_runs_and_not_again_at_the_end(
    ppy_home, source_repo, monkeypatch
) -> None:
    from papaya_agent_runtime import tool_learning

    shape = [
        {"tool_name": "Bash", "tool_use_id": f"toolu_{n}", "tool_input": {"command": command}}
        for n, command in enumerate(("cd src && ls", "git log | head -3"))
    ]

    class Denying(FakeProvider):
        """Two command-shape denials as the session starts, and the same two in its result."""

        def live_denial(self, event, events):
            if event.kind == "session":
                return shape[0]
            if event.kind == "assistant" and len(events) == 2:
                return shape[1]
            return None

        def permission_denials(self, events):
            return shape

    steered: list[int] = []
    monkeypatch.setattr(tool_learning, "steer_worker", lambda task, message: steered.append(task))
    spec, run_id, task_id = _dispatch_spec(source_repo)
    RunnerGuardian(Denying()).run(spec)

    conn = init_db()
    kinds = [
        row["kind"]
        for row in conn.execute("SELECT kind FROM events WHERE task_id = ? ORDER BY id", (task_id,))
    ]
    assert kinds.count(tool_learning.PERMISSION_DENIED) == 2
    assert kinds.count(tool_learning.DENIAL_STEER) == 1
    assert steered == [task_id]
    # Both, and the steer, before the worker's result line was read.
    assert kinds.index(tool_learning.DENIAL_STEER) < kinds.index("worker_result")


def test_fake_worker_failure_is_not_silent_success(ppy_home, source_repo) -> None:
    spec, run_id, task_id = _dispatch_spec(source_repo, instructions="FAIL")
    result = RunnerGuardian(FakeProvider()).run(spec)
    assert result.status == "failed"
    conn = init_db()
    assert store.get_task(conn, task_id)["status"] == "failed"
