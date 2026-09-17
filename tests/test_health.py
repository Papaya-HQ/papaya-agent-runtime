"""Hermetic tests for worker health: alive / quiet / dead, and the supervisor poller."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from papaya_agent_runtime import health, repos
from papaya_agent_runtime.cli import main
from papaya_agent_runtime.config import MMConfig, save_config
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.supervisor.server import SupervisorServer


@pytest.fixture
def ppy_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PPY_HOME", str(tmp_path / ".ppy"))
    return tmp_path / ".ppy"


def _in_flight_task(conn, *, title="build", pid=4242, heard_ago: timedelta | None = None):
    run_id = store.create_run(conn, title)
    task_id = store.add_task(conn, run_id=run_id, title=title)
    store.set_task_status(conn, task_id, "in_progress")
    store.register_runner(conn, runner_id=f"r{task_id}", task_id=task_id, provider="claude")
    store.update_runner(conn, f"r{task_id}", pid=pid, status="running")
    if heard_ago is not None:
        at = (datetime.now(UTC) - heard_ago).isoformat()
        conn.execute(
            "INSERT INTO events (run_id, task_id, seq, kind, payload, created_at) "
            "VALUES (?, ?, 1, 'worker_text', '{}', ?)",
            (run_id, task_id, at),
        )
        conn.commit()
    return task_id


def test_verdicts(ppy_home, monkeypatch) -> None:
    conn = init_db()
    monkeypatch.setattr(health, "_pid_alive", lambda pid: pid == 4242)
    alive = _in_flight_task(conn, title="alive", heard_ago=timedelta(minutes=1))
    quiet = _in_flight_task(conn, title="quiet", heard_ago=timedelta(minutes=40))
    dead = _in_flight_task(conn, title="dead", pid=1, heard_ago=timedelta(minutes=2))

    entries = {e["task_id"]: e for e in health.check(conn, quiet_after=timedelta(minutes=15))}

    assert entries[alive]["verdict"] == "alive"
    assert entries[quiet]["verdict"] == "quiet"
    assert entries[quiet]["silent_seconds"] >= 40 * 60
    assert entries[dead]["verdict"] == "dead"
    assert "QUIET for 40m" in health.describe(entries[quiet])
    assert "DEAD" in health.describe(entries[dead])


def test_last_heard_falls_back_to_task_row(ppy_home, monkeypatch) -> None:
    conn = init_db()
    monkeypatch.setattr(health, "_pid_alive", lambda pid: True)
    task_id = _in_flight_task(conn)  # no events at all
    (entry,) = health.check(conn, quiet_after=timedelta(minutes=15))
    assert entry["task_id"] == task_id
    assert entry["verdict"] == "alive"
    assert entry["last_heard"] is not None


def test_flag_quiet_workers_once_per_episode(ppy_home, monkeypatch) -> None:
    conn = init_db()
    monkeypatch.setattr(health, "_pid_alive", lambda pid: True)
    task_id = _in_flight_task(conn, heard_ago=timedelta(minutes=30))
    flagged: set[int] = set()
    threshold = timedelta(minutes=15)

    first = health.flag_quiet_workers(conn, quiet_after=threshold, already_flagged=flagged)
    assert [e["task_id"] for e in first] == [task_id]
    assert flagged == {task_id}
    # Second tick: still quiet, but already flagged — no duplicate event.
    assert health.flag_quiet_workers(conn, quiet_after=threshold, already_flagged=flagged) == []
    events = conn.execute("SELECT kind FROM events WHERE task_id = ?", (task_id,)).fetchall()
    assert [e["kind"] for e in events].count("worker_quiet") == 1

    # The flagged event is actionable, so `ppy run`/`ppy wait` surface it.
    run_id = store.get_task(conn, task_id)["run_id"]
    assert any(e["kind"] == "worker_quiet" for e in store.actionable_events(conn, run_id))

    # The worker speaks again → the episode ends and a relapse is flagged anew.
    store.append_event(conn, kind="worker_text", payload={}, run_id=run_id, task_id=task_id)
    assert health.flag_quiet_workers(conn, quiet_after=threshold, already_flagged=flagged) == []
    assert flagged == set()


def test_supervisor_tick_polls_health(ppy_home, monkeypatch) -> None:
    conn = init_db()
    monkeypatch.setattr(health, "_pid_alive", lambda pid: True)
    task_id = _in_flight_task(conn, heard_ago=timedelta(hours=1))
    server = SupervisorServer(socket_path=str(ppy_home / "t.sock"))

    server._tick_health()

    kinds = [
        r["kind"] for r in conn.execute("SELECT kind FROM events WHERE task_id = ?", (task_id,))
    ]
    assert "worker_quiet" in kinds
    assert server._quiet_flagged == {task_id}


def test_config_health_threshold_and_cli(ppy_home, monkeypatch, capsys) -> None:
    save_config(MMConfig())
    assert health.quiet_threshold() == timedelta(minutes=15)
    assert main(["config", "health", "--quiet-minutes", "5"]) == 0
    assert "5m of silence" in capsys.readouterr().out
    assert health.quiet_threshold() == timedelta(minutes=5)

    conn = init_db()
    monkeypatch.setattr(health, "_pid_alive", lambda pid: True)
    _in_flight_task(conn, heard_ago=timedelta(minutes=10))
    assert main(["health"]) == 1  # quiet under the 5m threshold → non-zero
    assert "QUIET" in capsys.readouterr().out
    assert main(["health", "--quiet-minutes", "30"]) == 0
    assert "alive" in capsys.readouterr().out


def test_health_with_no_workers(ppy_home, capsys) -> None:
    init_db()
    assert main(["health"]) == 0
    assert "no workers in flight" in capsys.readouterr().out


def test_dispatch_capacity_boundaries_name_the_remedies(ppy_home, monkeypatch) -> None:
    cfg = MMConfig()
    cfg.health.max_stale_stacks = 4
    save_config(cfg)
    monkeypatch.setattr(
        health,
        "pool_capacity",
        lambda repo=None, conn=None: {
            "backend": "treehouse",
            "known": True,
            "free_slots": 0,
            "total_slots": 16,
        },
    )
    monkeypatch.setattr(health.compose, "prunable_stacks", lambda conn=None: [])
    with pytest.raises(health.DispatchHealthError, match="ppy worktree prune"):
        health.require_dispatch_capacity()

    monkeypatch.setattr(
        health,
        "pool_capacity",
        lambda repo=None, conn=None: {
            "backend": "treehouse",
            "known": True,
            "free_slots": 1,
            "total_slots": 16,
        },
    )
    monkeypatch.setattr(health.compose, "prunable_stacks", lambda conn=None: [{}] * 4)
    assert health.require_dispatch_capacity()["stale_stacks"] == 4

    monkeypatch.setattr(health.compose, "prunable_stacks", lambda conn=None: [{}] * 5)
    with pytest.raises(health.DispatchHealthError, match="ppy task close"):
        health.require_dispatch_capacity()


def _treehouse_rows(count: int, *, available: set[int] | None = None) -> list[dict]:
    available = available or set()
    return [
        {
            "name": str(index + 1),
            "path": f"/pool/{index + 1}/repo",
            "status": "available" if index in available else "leased",
            "lease_id": None if index in available else f"lease-{index + 1}",
            "lease_holder": None if index in available else f"task-{index + 1}",
            "leased_at": None,
            "processes": [],
        }
        for index in range(count)
    ]


def test_treehouse_pool_capacity_counts_room_to_grow(ppy_home, source_repo, monkeypatch) -> None:
    added = repos.add_repo(source_repo)
    repo = store.get_repo(init_db(), added.name)
    Path(repo["local_path"], "treehouse.toml").write_text("max_trees = 4\n")
    monkeypatch.setenv("PPY_LEASE_BACKEND", "treehouse")
    rows = _treehouse_rows(3)
    monkeypatch.setattr(health, "_run_treehouse_status", lambda cwd: (0, json.dumps(rows)))

    capacity = health.pool_capacity(added.name)
    assert capacity == {
        "backend": "treehouse",
        "known": True,
        "free_slots": 1,
        "total_slots": 4,
    }
    assert health.require_dispatch_capacity(added.name)["pool"]["free_slots"] == 1

    rows[:] = _treehouse_rows(4)
    with pytest.raises(health.DispatchHealthError, match="ppy worktree prune"):
        health.require_dispatch_capacity(added.name)


def test_treehouse_pool_capacity_adds_reusable_and_growable_slots(
    ppy_home, source_repo, monkeypatch
) -> None:
    added = repos.add_repo(source_repo)
    repo = store.get_repo(init_db(), added.name)
    Path(repo["local_path"], "treehouse.toml").write_text("max_trees = 4\n")
    monkeypatch.setenv("PPY_LEASE_BACKEND", "treehouse")
    rows = _treehouse_rows(2, available={0})
    monkeypatch.setattr(health, "_run_treehouse_status", lambda cwd: (0, json.dumps(rows)))

    assert health.pool_capacity(added.name)["free_slots"] == 3


def test_treehouse_pool_capacity_is_unknown_for_unreadable_ceiling(
    ppy_home, source_repo, monkeypatch
) -> None:
    added = repos.add_repo(source_repo)
    repo = store.get_repo(init_db(), added.name)
    Path(repo["local_path"], "treehouse.toml").write_text("max_trees = nope\n")
    monkeypatch.setenv("PPY_LEASE_BACKEND", "treehouse")
    monkeypatch.setattr(
        health,
        "_run_treehouse_status",
        lambda cwd: (0, json.dumps(_treehouse_rows(16))),
    )

    capacity = health.pool_capacity(added.name)
    assert capacity["known"] is False
    assert capacity["free_slots"] is None
    assert health.require_dispatch_capacity(added.name)["pool"]["known"] is False


def test_repo_without_compose_has_no_stale_stack_count(ppy_home, source_repo, monkeypatch) -> None:
    added = repos.add_repo(source_repo)
    monkeypatch.setattr(
        health,
        "pool_capacity",
        lambda repo=None, conn=None: {
            "backend": "git",
            "known": False,
            "free_slots": None,
            "total_slots": None,
        },
    )
    monkeypatch.setattr(
        health.compose,
        "prunable_stacks",
        lambda conn=None: (_ for _ in ()).throw(AssertionError("no compose probe")),
    )
    assert health.dispatch_snapshot(added.name)["stale_stacks"] is None


def test_status_prints_usage_only_above_the_task_and_review_ceilings(ppy_home, capsys) -> None:
    cfg = MMConfig()
    cfg.usage.input_ceiling_per_task = 100
    cfg.usage.input_ceiling_per_review = 30
    save_config(cfg)
    conn = init_db()
    run_id = store.create_run(conn, "usage")
    under = store.add_task(conn, run_id=run_id, title="under")
    over = store.add_task(conn, run_id=run_id, title="over")
    review = store.add_task(conn, run_id=run_id, title="review round", role="reviewer")
    for task_id, tokens in ((under, 100), (over, 101), (review, 31)):
        store.record_usage(
            conn,
            run_id=run_id,
            task_id=task_id,
            provider="codex",
            model="m",
            reasoning="low",
            input_tokens=tokens,
            output_tokens=1,
        )
    assert main(["status"]) == 0
    output = capsys.readouterr().out
    assert f"task {under}" not in output
    assert f"task {over}" in output and "task ceiling of 100" in output
    assert f"task {review}" in output and "review ceiling of 30" in output


def test_the_first_health_tick_runs_however_long_the_machine_has_been_up(
    ppy_home, monkeypatch
) -> None:
    """`time.monotonic()` is time since boot, so a 0.0 sentinel is a live tick at boot.

    With `_last_health_tick = 0.0`, the guard `monotonic() - 0.0 < 60` is true for the
    first minute of a machine's uptime — so a supervisor started on a freshly booted
    box silently polled nothing, and prepared no assessments, until the box had been
    up a minute. It surfaced as a flaky CI test on a fresh runner (2026-09-15).
    """
    from papaya_agent_runtime.supervisor import server as server_mod

    monkeypatch.setattr(health, "_pid_alive", lambda pid: True)
    conn = init_db()
    task_id = _in_flight_task(conn, heard_ago=timedelta(hours=1))
    monkeypatch.setattr(server_mod.time, "monotonic", lambda: 0.5)  # boot, near enough

    server = SupervisorServer(socket_path=str(ppy_home / "boot.sock"))
    server._tick_health()

    kinds = [
        r["kind"] for r in conn.execute("SELECT kind FROM events WHERE task_id = ?", (task_id,))
    ]
    assert "worker_quiet" in kinds


def test_a_second_tick_inside_the_interval_is_skipped(ppy_home, monkeypatch) -> None:
    """The rate limit still has to hold, or every loop iteration re-polls."""
    from papaya_agent_runtime.supervisor import server as server_mod

    monkeypatch.setattr(health, "_pid_alive", lambda pid: True)
    conn = init_db()
    _in_flight_task(conn, heard_ago=timedelta(hours=1))
    clock = {"now": 1000.0}
    monkeypatch.setattr(server_mod.time, "monotonic", lambda: clock["now"])

    server = SupervisorServer(socket_path=str(ppy_home / "rate.sock"))
    server._tick_health()
    first = server._last_health_tick

    clock["now"] += server_mod.TICK_SECONDS - 1
    server._tick_health()
    assert server._last_health_tick == first  # skipped

    clock["now"] += 2
    server._tick_health()
    assert server._last_health_tick != first  # ran


def test_a_ticket_task_is_never_an_in_flight_worker(tmp_path, monkeypatch) -> None:
    """Ticket tasks are serve's hold records; the heartbeat listed fifteen as dead workers."""
    from papaya_agent_runtime import health
    from papaya_agent_runtime.state import init_db, store

    monkeypatch.setenv("PPY_HOME", str(tmp_path / ".ppy"))
    conn = init_db()
    run_id = store.create_run(conn, "r")
    ticket = store.add_task(conn, run_id=run_id, title="ticket")
    store.set_task_phase(conn, ticket, "dispatched")
    worker = store.add_task(conn, run_id=run_id, title="build")

    assert [e["task_id"] for e in health.check(conn)] == [worker]
