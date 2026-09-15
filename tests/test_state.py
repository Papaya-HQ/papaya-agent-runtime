"""Hermetic tests for the SQLite schema and store helpers."""

from __future__ import annotations

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
