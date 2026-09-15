"""Hermetic tests for the Lavish artifact loop and lifecycle hooks."""

from __future__ import annotations

import json
from pathlib import Path

from papaya_agent_runtime import hooks
from papaya_agent_runtime.artifacts import create_artifact, read_feedback
from papaya_agent_runtime.state import init_db, store


def _seed_due_assessment(conn) -> None:
    for index in range(5):
        run_id = store.create_run(conn, f"completed objective {index}")
        store.set_run_status(conn, run_id, "completed")


def test_artifact_roundtrip(ppy_home) -> None:
    conn = init_db()
    run_id = store.create_run(conn, "review this")
    art = create_artifact(
        run_id,
        "Design review",
        [{"heading": "Naming", "body": "getX vs getCurrentX", "options": ["keep", "rename"]}],
    )
    html = Path(art.path).read_text(encoding="utf-8")
    assert "Design review" in html
    assert "getX vs getCurrentX" in html

    # Session starts open.
    first = read_feedback(art.id)
    assert first["session_state"] == "open"

    # User fills in the sidecar and closes the session.
    fb = Path(art.feedback_path)
    data = json.loads(fb.read_text())
    data["responses"] = {"q0": "rename"}
    data["session_state"] = "closed"
    fb.write_text(json.dumps(data))

    closed = read_feedback(art.id)
    assert closed["session_state"] == "closed"
    assert closed["feedback"]["responses"] == {"q0": "rename"}

    # The DB reflects the closed state.
    row = conn.execute("SELECT session_state FROM artifacts WHERE id = ?", (art.id,)).fetchone()
    assert row["session_state"] == "closed"


def test_artifact_emits_review_requested(ppy_home) -> None:
    conn = init_db()
    run_id = store.create_run(conn, "obj")
    create_artifact(run_id, "T", [])
    kinds = [e["kind"] for e in store.actionable_events(conn, run_id)]
    assert "review_requested" in kinds


def test_hook_records_lifecycle_event(ppy_home) -> None:
    conn = init_db()
    run_id = store.create_run(conn, "obj")
    task_id = store.add_task(conn, run_id=run_id, title="t")
    ack = hooks.handle_hook_stdin("Stop", json.dumps({"task_id": task_id, "reason": "turn end"}))
    assert ack["decision"] == "block"
    assert "ppy todo add" in ack["reason"]
    row = conn.execute(
        "SELECT kind, payload FROM events WHERE task_id = ? ORDER BY seq DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    assert row["kind"] == "hook_stop"
    assert json.loads(row["payload"])["reason"] == "turn end"


def test_hook_tolerates_bad_json(ppy_home) -> None:
    conn = init_db()
    store.create_run(conn, "obj")
    ack = hooks.handle_hook_stdin("session-start", "not json")
    assert ack == {}


def test_stop_hook_continues_due_assessment_once(ppy_home) -> None:
    conn = init_db()
    _seed_due_assessment(conn)

    first = hooks.handle_hook_stdin("Stop", "{}")
    recursive = hooks.handle_hook_stdin("Stop", json.dumps({"stop_hook_active": True}))

    assert first["decision"] == "block"
    assert "Periodic performance assessment" in first["reason"]
    assert "decision" not in recursive
    assert conn.execute("SELECT COUNT(*) FROM assessment_cycles").fetchone()[0] == 1


def test_session_start_hook_injects_pending_assessment_context(ppy_home) -> None:
    conn = init_db()
    _seed_due_assessment(conn)

    response = hooks.handle_hook_stdin("SessionStart", "{}")

    context = response["hookSpecificOutput"]["additionalContext"]
    assert "Periodic performance assessment" in context
    assert response["hookSpecificOutput"] == {
        "hookEventName": "SessionStart",
        "additionalContext": context,
    }
