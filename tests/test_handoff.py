"""Hermetic tests for `ppy handoff`: a snapshot file plus a short prompt that names it."""

from __future__ import annotations

import json

import pytest

from papaya_agent_runtime import board, handoff, memory
from papaya_agent_runtime.cli import main
from papaya_agent_runtime.state import init_db, store


@pytest.fixture
def ppy_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PPY_HOME", str(tmp_path / ".ppy"))
    return tmp_path / ".ppy"


@pytest.fixture
def no_supervisor(monkeypatch):
    monkeypatch.setattr(
        handoff, "supervisor_status", lambda: {"reachable": False, "socket": "/nowhere"}
    )


@pytest.fixture
def supervisor_up(monkeypatch):
    monkeypatch.setattr(
        handoff, "supervisor_status", lambda: {"reachable": True, "pid": 1, "socket": "/s"}
    )


def _seed(conn):
    repo_id = store.add_repo(
        conn,
        name="demo",
        origin="/tmp/demo",
        local_path="/tmp/demo-clone",
        default_branch="main",
        base_sha="abc123",
    )
    run_id = store.create_run(conn, "ship the widget")
    store.set_run_status(conn, run_id, "running")
    running = store.add_task(conn, run_id=run_id, title="build widget", repo_id=repo_id)
    store.set_task_status(conn, running, "in_progress")
    done = store.add_task(conn, run_id=run_id, title="widget tests", repo_id=repo_id)
    store.set_task_status(conn, done, "worker_done")
    blocked = store.add_task(conn, run_id=run_id, title="widget docs", repo_id=repo_id)
    store.set_task_status(conn, blocked, "blocked")
    store.append_event(
        conn,
        kind="blocked",
        payload={"task_id": blocked, "question": "v1 or v2 endpoint?"},
        run_id=run_id,
        task_id=blocked,
    )
    old_run = store.create_run(conn, "old thing")
    shipped = store.add_task(conn, run_id=old_run, title="shipped", repo_id=repo_id)
    store.set_task_status(conn, shipped, "delivered")
    return {"run_id": run_id, "running": running, "done": done, "blocked": blocked, "old": old_run}


def test_collect_buckets_open_runs_only(ppy_home, no_supervisor) -> None:
    conn = init_db()
    ids = _seed(conn)

    data = handoff.collect(conn)

    assert [r["id"] for r in data["open_runs"]] == [ids["run_id"]]
    run = data["open_runs"][0]
    assert [t["id"] for t in run["in_flight"]] == [ids["running"]]
    assert {t["id"] for t in run["needs_me"]} == {ids["done"], ids["blocked"]}
    blocked = next(t for t in run["needs_me"] if t["id"] == ids["blocked"])
    assert blocked["question"] == "v1 or v2 endpoint?"
    assert next(t for t in run["needs_me"] if t["id"] == ids["done"])["review"] == "not reviewed"
    assert data["supervisor"]["reachable"] is False
    assert [h["task_id"] for h in data["health"]] == [ids["running"]]
    assert data["health"][0]["verdict"] == "dead"  # no runner process was ever started


def test_warnings_cover_team_risks(ppy_home, no_supervisor) -> None:
    conn = init_db()
    ids = _seed(conn)

    warnings = handoff.warnings_for(handoff.collect(conn))
    text = "\n".join(warnings)

    assert "Supervisor is not running" in text and f"task(s) {ids['running']}" in text
    assert "Worker process gone" in text
    assert 'is idle waiting on an answer: "v1 or v2 endpoint?"' in text
    assert "finished and waiting on review" in text
    assert "No next step is recorded" in text


def test_alive_worker_warning_when_supervisor_up(ppy_home, supervisor_up, monkeypatch) -> None:
    conn = init_db()
    ids = _seed(conn)
    monkeypatch.setattr(handoff.health, "_pid_alive", lambda pid: True)
    store.register_runner(conn, runner_id="r1", task_id=ids["running"], provider="claude")
    store.update_runner(conn, "r1", pid=4242, status="running")
    store.append_event(
        conn, kind="worker_text", payload={}, run_id=ids["run_id"], task_id=ids["running"]
    )

    warnings = handoff.warnings_for(handoff.collect(conn))
    text = "\n".join(warnings)

    assert "1 worker(s) still running" in text
    assert "Supervisor is" not in text
    assert "Worker process gone" not in text


def test_snapshot_goes_to_file_and_prompt_names_it(ppy_home, no_supervisor, monkeypatch) -> None:
    conn = init_db()
    ids = _seed(conn)
    a = board.add("review task 2 and deliver", run_id=ids["run_id"], conn=conn)
    b = board.add("answer task 3: v2", conn=conn)
    c = board.add("confirm the endpoint", blocked_on="user", conn=conn)
    store.append_event(
        conn,
        kind="worker_progress",
        payload={"task_id": ids["running"], "phase": "implement", "note": "wiring it up"},
        run_id=ids["run_id"],
        task_id=ids["running"],
    )
    monkeypatch.chdir(ppy_home.parent)

    result = handoff.build_handoff(conn)
    prompt = result["prompt"]
    snapshot_path = memory.memory_dir() / "handoff.md"
    snapshot = snapshot_path.read_text()

    # The file carries the snapshot: ledger, open runs and tasks, known risks.
    assert result["snapshot"] == str(snapshot_path)
    assert snapshot.startswith("# Handoff snapshot")
    assert (
        f"- Next (todo ledger): #{a} review task 2 and deliver; #{b} answer task 3: v2" in snapshot
    )
    assert f"- Waiting (todo ledger): #{c} confirm the endpoint (waiting on user)" in snapshot
    assert f'Run {ids["run_id"]} "ship the widget"' in snapshot
    assert '"widget docs" (demo) — blocked · question: "v1 or v2 endpoint?"' in snapshot
    assert '"build widget" (demo) — in_progress · phase implement ("wiring it up")' in snapshot
    assert "## Known risks at handoff" in snapshot
    assert "No next step is recorded" not in snapshot

    # The prompt stays short and points at the file instead of repeating it.
    assert "Pick up where we left off" in prompt
    assert "ppy supervisor status" in prompt
    assert "ppy reconcile" in prompt
    assert "ppy health" in prompt
    assert "ppy todo list" in prompt
    assert "`.ppy/memory/handoff.md`" in prompt
    assert "1 open run(s), 3 ledger item(s)" in prompt and "known risk(s)" in prompt
    assert "ship the widget" not in prompt
    assert "v1 or v2 endpoint?" not in prompt
    assert "Known risks at handoff" not in prompt
    assert "greet me with one dry line" in prompt
    assert len(prompt.splitlines()) < 12


def test_snapshot_path_is_absolute_when_outside_cwd(ppy_home, no_supervisor, tmp_path, monkeypatch):
    conn = init_db()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    prompt = handoff.build_handoff(conn)["prompt"]
    assert f"`{memory.memory_dir() / 'handoff.md'}`" in prompt


def test_session_context_for_hooks(ppy_home, no_supervisor) -> None:
    conn = init_db()
    assert handoff.render_session_context(handoff.collect(conn)) is None
    ids = _seed(conn)
    context = handoff.render_session_context(handoff.collect(conn))
    assert context.startswith("Papaya Agent Runtime pickup context")
    assert f"Run {ids['run_id']}" in context
    assert "Attention:" in context


def test_handoff_with_nothing_open_still_returns_a_prompt(ppy_home, no_supervisor) -> None:
    result = handoff.build_handoff()
    assert "nothing in flight; nothing waiting on me" in result["prompt"]
    assert result["warnings"] == []
    assert "Known risks" not in result["prompt"]
    assert (
        "- Nothing in flight; nothing waiting on me."
        in (memory.memory_dir() / "handoff.md").read_text()
    )


def test_cli_handoff_prints_prompt_and_warnings(ppy_home, no_supervisor, capsys) -> None:
    conn = init_db()
    _seed(conn)

    assert main(["handoff"]) == 0
    captured = capsys.readouterr()
    assert "Pick up where we left off" in captured.out
    assert captured.err.startswith("warning: ")

    assert main(["handoff", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"prompt", "warnings", "snapshot", "data"}
    assert payload["snapshot"].endswith("handoff.md")
    assert payload["data"]["open_runs"][0]["objective"] == "ship the widget"
