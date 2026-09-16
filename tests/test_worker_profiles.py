"""Concrete worker profiles are resolved, persisted, and revalidated."""

from __future__ import annotations

import os

import pytest

from conftest import wait_until
from papaya_agent_runtime import repos, stacks
from papaya_agent_runtime.config import MMConfig, WorkerCeiling, save_config
from papaya_agent_runtime.providers.fake import FakeProvider
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor import core
from papaya_agent_runtime.supervisor.core import Supervisor, SupervisorError


def _config(max_model: str = "gpt-5.6-sol", default_model: str = "gpt-5.6-sol") -> None:
    save_config(
        MMConfig(worker=WorkerCeiling("codex", max_model, "xhigh", default_model, "high", 2))
    )


def _capture_runs(supervisor: Supervisor, monkeypatch) -> list:
    captured = []
    monkeypatch.setattr(core, "_adapter_for", lambda provider: FakeProvider())

    def capture(runner, spec, execution=None):
        captured.append(spec)
        supervisor._release(execution)

    monkeypatch.setattr(supervisor, "_run_task", capture)
    return captured


def _wait_for(predicate, timeout: float = 5.0) -> None:
    wait_until(predicate, timeout, what="captured worker", interval=0.01)


def test_omitted_real_worker_choices_resolve_and_persist_before_start(
    ppy_home, source_repo, monkeypatch
) -> None:
    _config()
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    captured = _capture_runs(supervisor, monkeypatch)

    response = supervisor.dispatch_task(repo=added.name, title="default profile", provider="codex")
    _wait_for(lambda: captured)
    task = store.get_task(init_db(), response["task_id"])
    assert (captured[0].model, captured[0].reasoning) == ("gpt-5.6-sol", "high")
    assert (task["model"], task["reasoning"]) == ("gpt-5.6-sol", "high")


def test_different_real_provider_is_refused_before_task_creation(ppy_home, source_repo) -> None:
    _config()
    added = repos.add_repo(source_repo)
    with pytest.raises(SupervisorError, match="does not match"):
        Supervisor().dispatch_task(repo=added.name, title="wrong provider", provider="claude")
    assert init_db().execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_resume_revalidates_stored_profile_before_mutating_task(
    ppy_home, source_repo, monkeypatch
) -> None:
    _config()
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    captured = _capture_runs(supervisor, monkeypatch)
    response = supervisor.dispatch_task(repo=added.name, title="old profile", provider="codex")
    _wait_for(lambda: captured)
    task_id = response["task_id"]
    conn = init_db()
    store.set_task_status(conn, task_id, "worker_stopped")
    before = dict(store.get_task(conn, task_id))

    _config(max_model="gpt-5.6-terra", default_model="gpt-5.6-terra")
    with pytest.raises(SupervisorError, match="exceeds ceiling"):
        supervisor.resume_task(task_id, "continue")
    after = dict(store.get_task(conn, task_id))
    assert after["status"] == before["status"]
    assert after["model"] == before["model"]
    assert not conn.execute(
        "SELECT 1 FROM events WHERE task_id = ? AND kind = 'resumed'", (task_id,)
    ).fetchone()


def test_resume_resolves_legacy_null_profile_and_persists_it(
    ppy_home, source_repo, monkeypatch
) -> None:
    _config()
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    captured = _capture_runs(supervisor, monkeypatch)
    response = supervisor.dispatch_task(repo=added.name, title="legacy profile", provider="codex")
    _wait_for(lambda: len(captured) == 1)
    task_id = response["task_id"]
    conn = init_db()
    store.update_task_fields(conn, task_id, model=None, reasoning=None)
    store.set_task_status(conn, task_id, "worker_stopped")
    monkeypatch.setattr(stacks, "sync_worktree_with_remote", lambda ignored: None)

    supervisor.resume_task(task_id, "continue")
    _wait_for(lambda: len(captured) == 2)
    task = store.get_task(conn, task_id)
    assert (captured[-1].model, captured[-1].reasoning) == ("gpt-5.6-sol", "high")
    assert (task["model"], task["reasoning"]) == ("gpt-5.6-sol", "high")


def test_disallowed_stored_profile_refuses_live_steer_before_interrupt(
    ppy_home, source_repo, monkeypatch
) -> None:
    _config()
    added = repos.add_repo(source_repo)
    supervisor = Supervisor()
    captured = _capture_runs(supervisor, monkeypatch)
    response = supervisor.dispatch_task(repo=added.name, title="live old profile", provider="codex")
    _wait_for(lambda: captured)
    task_id = response["task_id"]
    conn = init_db()
    store.set_task_status(conn, task_id, "in_progress")
    store.register_runner(
        conn, runner_id="still-live", task_id=task_id, provider="codex", pid=os.getpid()
    )
    store.update_runner(conn, "still-live", status="running")
    _config(max_model="gpt-5.6-terra", default_model="gpt-5.6-terra")

    with pytest.raises(SupervisorError, match="exceeds ceiling"):
        supervisor.steer_task(task_id, "change course")
    assert store.get_runner(conn, "still-live")["superseded_at"] is None
    assert not conn.execute(
        "SELECT 1 FROM events WHERE task_id = ? AND kind = 'steer'", (task_id,)
    ).fetchone()
