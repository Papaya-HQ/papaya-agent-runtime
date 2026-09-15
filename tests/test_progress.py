"""Hermetic tests for structured worker progress and the missing-plan gate."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from papaya_agent_runtime import health, memory, progress
from papaya_agent_runtime.cli import main
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor.runner import worker_env


@pytest.fixture
def ppy_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PPY_HOME", str(tmp_path / ".ppy"))
    return tmp_path / ".ppy"


def _task(conn, *, status="in_progress", title="build", ends_at="done"):
    repo_id = store.add_repo(
        conn, name="demo", origin="/o", local_path="/l", default_branch="main", base_sha="a"
    )
    run_id = store.create_run(conn, "ship it")
    task_id = store.add_task(conn, run_id=run_id, title=title, repo_id=repo_id, ends_at=ends_at)
    store.set_task_status(conn, task_id, status)
    return run_id, task_id


def test_record_latest_history_and_repo_log(ppy_home) -> None:
    conn = init_db()
    run_id, task_id = _task(conn)

    progress.record(task_id, phase="plan", note="add a widget module", conn=conn)
    progress.record(task_id, phase="implement", note="module + tests", conn=conn)

    latest = progress.latest(task_id, conn=conn)
    assert latest["phase"] == "implement" and latest["note"] == "module + tests"
    assert [e["phase"] for e in progress.history(task_id, conn=conn)] == ["implement", "plan"]
    assert store.has_progress_phase(conn, task_id, "plan")

    log = progress.render_repo_log("demo", conn=conn)
    assert f'task {task_id} "build" · implement — module + tests' in log
    assert log.index("implement") < log.index("plan")  # newest first
    assert "(repo not registered)" in progress.render_repo_log("nope", conn=conn)

    kinds = [
        r["kind"] for r in conn.execute("SELECT kind FROM events WHERE task_id = ?", (task_id,))
    ]
    assert kinds == ["worker_progress", "worker_progress"]
    # Progress counts as "heard from" for the health poller.
    assert health.last_heard(conn, task_id) is not None


def test_record_validation(ppy_home) -> None:
    conn = init_db()
    _, task_id = _task(conn)
    with pytest.raises(progress.ProgressError):
        progress.record(task_id, phase="thinking", conn=conn)
    with pytest.raises(progress.ProgressError):
        progress.record(task_id, phase="plan", note="  ", conn=conn)
    with pytest.raises(progress.ProgressError):
        progress.record(999, phase="implement", conn=conn)


def test_review_only_task_refuses_done_without_changing_state(ppy_home) -> None:
    conn = init_db()
    _, task_id = _task(conn, ends_at="review")
    before = dict(store.get_task(conn, task_id))
    events_before = conn.execute(
        "SELECT COUNT(*) FROM events WHERE task_id = ?", (task_id,)
    ).fetchone()[0]

    with pytest.raises(progress.ProgressError, match="--ends-at review"):
        progress.record(task_id, phase="done", note="finished", conn=conn)

    assert dict(store.get_task(conn, task_id)) == before
    assert (
        conn.execute("SELECT COUNT(*) FROM events WHERE task_id = ?", (task_id,)).fetchone()[0]
        == events_before
    )
    assert progress.record(task_id, phase="review", note="ready", conn=conn)


def test_cli_progress_task_and_memory_show(ppy_home, capsys) -> None:
    conn = init_db()
    _, task_id = _task(conn)
    assert main(["progress", str(task_id), "--phase", "plan", "--note", "do the thing"]) == 0
    assert main(["progress", str(task_id)]) == 0
    assert "plan — do the thing" in capsys.readouterr().out
    assert main(["progress", str(task_id), "--history", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["phase"] == "plan"
    assert main(["progress", str(task_id), "--phase", "plan"]) == 1
    assert "needs a note" in capsys.readouterr().err

    memory.seed_repo_memory("demo")
    assert main(["memory", "show", "--repo", "demo"]) == 0
    out = capsys.readouterr().out
    assert "# ---- progress (from `ppy progress`) ----" in out
    assert "do the thing" in out
    assert "follow-ups" in out


def test_worker_gets_mm_on_path_and_a_pinned_home(ppy_home) -> None:
    env = worker_env({"PATH": "/usr/bin"})
    assert env["PPY_HOME"] == str(ppy_home)
    first = env["PATH"].split(os.pathsep)[0]
    assert first.endswith("/bin") and os.path.exists(os.path.join(first, "ppy"))
    assert env["PATH"].endswith("/usr/bin")


def test_worker_context_tells_the_worker_how_to_report(ppy_home) -> None:
    text = memory.worker_context("demo", task_id=7)
    assert "ppy progress 7 --phase plan --note" in text
    assert "--phase implement|test|review" in text
    assert str(memory.repo_notes_path("demo")) in text


def test_missing_plan_is_flagged_once_after_grace(ppy_home) -> None:
    conn = init_db()
    run_id, task_id = _task(conn)
    store.register_runner(conn, runner_id="r1", task_id=task_id, provider="claude")
    flagged: set[int] = set()
    grace = timedelta(minutes=10)
    later = datetime.now(UTC) + timedelta(minutes=11)

    # Inside the grace period: nothing.
    assert health.flag_missing_plans(conn, grace=grace, already_flagged=flagged) == []
    # After it: flagged exactly once, as an actionable event.
    assert health.flag_missing_plans(conn, grace=grace, already_flagged=flagged, now=later) == [
        task_id
    ]
    assert health.flag_missing_plans(conn, grace=grace, already_flagged=flagged, now=later) == []
    actionable = [e["kind"] for e in store.actionable_events(conn, run_id)]
    assert actionable.count("plan_missing") == 1

    # A task that did post a plan is never flagged.
    _, planned = _task_second(conn, run_id)
    progress.record(planned, phase="plan", note="approach", conn=conn)
    assert health.flag_missing_plans(conn, grace=grace, already_flagged=flagged, now=later) == []
    assert planned in flagged  # satisfied, remembered so it is never re-checked


def _task_second(conn, run_id):
    task_id = store.add_task(conn, run_id=run_id, title="second")
    store.set_task_status(conn, task_id, "in_progress")
    return run_id, task_id


def test_supervisor_tick_flags_missing_plans(ppy_home, monkeypatch) -> None:
    from papaya_agent_runtime.supervisor.server import SupervisorServer

    conn = init_db()
    _, task_id = _task(conn)
    old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (old, task_id))
    conn.commit()
    server = SupervisorServer(socket_path=str(ppy_home / "t.sock"))
    server._tick_health()
    kinds = [
        r["kind"] for r in conn.execute("SELECT kind FROM events WHERE task_id = ?", (task_id,))
    ]
    assert "plan_missing" in kinds
    assert server._plan_flagged == {task_id}
