"""Hermetic tests for the SQLite schema and store helpers."""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from papaya_agent_runtime.state import db as state_db
from papaya_agent_runtime.state import store
from papaya_agent_runtime.state.db import SCHEMA_VERSION, init_db, schema_version


def test_init_db_idempotent(tmp_path) -> None:
    db = tmp_path / "state.db"
    conn = init_db(db)
    assert schema_version(conn) == SCHEMA_VERSION
    conn.close()
    # Re-init must not error or lose data.
    conn2 = init_db(db)
    assert schema_version(conn2) == SCHEMA_VERSION
    conn2.close()


def test_second_writer_waits_past_driver_default_for_held_lock(tmp_path) -> None:
    db = tmp_path / "state.db"
    holder = state_db.connect(db)
    holder.execute("CREATE TABLE lock_probe (value TEXT NOT NULL)")
    holder.commit()
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO lock_probe VALUES ('held')")

    attempting_write = threading.Event()
    outcome: list[object] = []

    def write_after_holder() -> None:
        conn = state_db.connect(db)
        try:
            attempting_write.set()
            started = time.monotonic()
            conn.execute("INSERT INTO lock_probe VALUES ('waited')")
            conn.commit()
            outcome.append(time.monotonic() - started)
        except Exception as exc:  # pragma: no cover - asserted below with context
            outcome.append(exc)
        finally:
            conn.close()

    writer = threading.Thread(target=write_after_holder)
    writer.start()
    assert attempting_write.wait(timeout=2)
    time.sleep(5.2)  # exceed sqlite3.connect's old five-second default
    assert writer.is_alive(), "the second writer should still be waiting for the held lock"
    holder.commit()
    writer.join(timeout=5)
    holder.close()

    assert not writer.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], float), outcome[0]
    assert outcome[0] >= 5
    conn = state_db.connect(db)
    assert [
        row["value"]
        for row in conn.execute("SELECT value FROM lock_probe ORDER BY rowid").fetchall()
    ] == ["held", "waited"]


def test_busy_timeout_override_applies_to_driver_and_pragma(tmp_path, monkeypatch) -> None:
    real_connect = sqlite3.connect
    driver_timeouts: list[float] = []

    def tracked_connect(*args, **kwargs):
        driver_timeouts.append(kwargs["timeout"])
        return real_connect(*args, **kwargs)

    monkeypatch.setenv(state_db.SQLITE_BUSY_TIMEOUT_ENV, "12.5")
    monkeypatch.setattr(state_db.sqlite3, "connect", tracked_connect)
    conn = state_db.connect(tmp_path / "state.db")

    assert driver_timeouts == [12.5]
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 12_500


@pytest.mark.parametrize("configured", [None, "not-a-number", "0", "-1", "nan", "inf", "0.0001"])
def test_absent_or_invalid_busy_timeout_override_uses_finite_default(
    tmp_path, monkeypatch, configured
) -> None:
    if configured is None:
        monkeypatch.delenv(state_db.SQLITE_BUSY_TIMEOUT_ENV, raising=False)
    else:
        monkeypatch.setenv(state_db.SQLITE_BUSY_TIMEOUT_ENV, configured)
    conn = state_db.connect(tmp_path / f"state-{configured or 'unset'}.db")

    assert state_db._busy_timeout_seconds() == state_db.SQLITE_BUSY_TIMEOUT_SECONDS
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == round(
        state_db.SQLITE_BUSY_TIMEOUT_SECONDS * 1000
    )


def test_schema_adds_terminal_phase_and_repo_environment_columns(tmp_path) -> None:
    conn = init_db(tmp_path / "state.db")
    task_columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    repo_columns = {row[1] for row in conn.execute("PRAGMA table_info(repos)")}
    assert "ends_at" in task_columns
    assert {
        "db_url_template",
        "test_db_url_template",
        "source_line_ceiling",
        "needs_elevated_localhost",
    } <= repo_columns


def test_existing_tasks_migrate_to_done_terminal_phase(tmp_path) -> None:
    db = tmp_path / "state.db"
    conn = init_db(db)
    repo_id = store.add_repo(
        conn,
        name="demo",
        origin="/tmp/demo",
        local_path="/tmp/demo-clone",
        default_branch="main",
        base_sha="abc123",
    )
    run_id = store.create_run(conn, "existing run")
    task_id = store.add_task(conn, run_id=run_id, title="existing task", repo_id=repo_id)
    conn.execute("ALTER TABLE tasks DROP COLUMN ends_at")
    conn.commit()
    conn.close()

    migrated = init_db(db)
    assert migrated.execute("SELECT ends_at FROM tasks WHERE id = ?", (task_id,)).fetchone()[0] == (
        "done"
    )


def test_repo_run_task_event_flow(tmp_path) -> None:
    conn = init_db(tmp_path / "state.db")
    repo_id = store.add_repo(
        conn,
        name="demo",
        origin="/tmp/demo",
        local_path="/tmp/demo-clone",
        default_branch="main",
        base_sha="abc123",
    )
    assert store.get_repo(conn, "demo")["id"] == repo_id

    run_id = store.create_run(conn, "make it work")
    t1 = store.add_task(conn, run_id=run_id, title="scout", repo_id=repo_id)
    t2 = store.add_task(conn, run_id=run_id, title="implement", repo_id=repo_id)
    store.add_dependency(conn, t2, t1)
    assert store.dependencies_of(conn, t2) == [t1]

    store.set_task_status(conn, t1, "worker_done")
    assert store.get_task(conn, t1)["status"] == "worker_done"

    e1 = store.append_event(conn, kind="dispatched", payload={"task": t1}, run_id=run_id)
    e2 = store.append_event(conn, kind="worker_done", payload={"task": t1}, run_id=run_id)
    assert e2 > e1
    after = store.events_after(conn, run_id, 0)
    assert [r["seq"] for r in after] == [1, 2]
    assert store.events_after(conn, run_id, 1)[0]["kind"] == "worker_done"
    conn.close()
