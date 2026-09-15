"""Hermetic tests for proactive manager performance assessments."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from papaya_agent_runtime import assessments, memory
from papaya_agent_runtime.config import AssessmentPolicy
from papaya_agent_runtime.state import init_db, store

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def _completed_run(conn, *, created_at: datetime) -> int:
    run_id = store.create_run(conn, "completed objective")
    store.set_run_status(conn, run_id, "completed")
    conn.execute(
        "UPDATE runs SET created_at = ?, updated_at = ? WHERE id = ?",
        (created_at.isoformat(), created_at.isoformat(), run_id),
    )
    conn.commit()
    return run_id


def _policy(**overrides: int | bool) -> AssessmentPolicy:
    values = {
        "enabled": True,
        "completed_runs": 5,
        "max_days": 14,
        "cooldown_days": 7,
        "minimum_runs": 2,
        "failure_trigger_count": 2,
        "max_actions": 3,
    }
    values.update(overrides)
    return AssessmentPolicy(**values)


def test_completed_run_trigger_creates_only_one_pending_cycle(ppy_home) -> None:
    conn = init_db()
    for offset in range(5):
        _completed_run(conn, created_at=NOW - timedelta(days=offset + 1))

    reason, evidence = assessments.due_reason(conn, policy=_policy(), now=NOW)
    assert reason == "completed_runs"
    assert evidence["runs"]["completed"] == 5

    first = assessments.ensure_due(conn, policy=_policy(), now=NOW)
    second = assessments.ensure_due(conn, policy=_policy(), now=NOW)

    assert first is not None
    assert first["trigger"] == "completed_runs"
    assert second is not None
    assert second["id"] == first["id"]
    assert conn.execute("SELECT COUNT(*) FROM assessment_cycles").fetchone()[0] == 1
    event = conn.execute("SELECT payload FROM events WHERE kind = 'assessment_ready'").fetchone()
    assert json.loads(event["payload"])["assessment_id"] == first["id"]

    conn.close()
    reopened = init_db()
    assert assessments.cycle_dict(reopened, first["id"])["status"] == "ready"
    reopened.close()


def test_elapsed_time_requires_minimum_evidence(ppy_home) -> None:
    conn = init_db()
    old = NOW - timedelta(days=15)
    _completed_run(conn, created_at=old)

    reason, _ = assessments.due_reason(conn, policy=_policy(), now=NOW)
    assert reason is None

    _completed_run(conn, created_at=old + timedelta(hours=1))
    reason, evidence = assessments.due_reason(conn, policy=_policy(), now=NOW)
    assert reason == "elapsed_time"
    assert evidence["window"]["elapsed_days"] == 15
    assert evidence["runs"]["completed"] == 2


def test_failure_trigger_uses_deterministic_task_evidence(ppy_home) -> None:
    conn = init_db()
    run_id = store.create_run(conn, "failed objective")
    for title in ("one", "two"):
        task_id = store.add_task(conn, run_id=run_id, title=title)
        store.set_task_status(conn, task_id, "failed")

    reason, evidence = assessments.due_reason(conn, policy=_policy(), now=NOW)

    assert reason == "failure"
    assert evidence["tasks"]["failed"] == 2
    assert evidence["failure_signals"] == 2


def test_disabled_policy_never_creates_cycle(ppy_home) -> None:
    conn = init_db()
    for offset in range(5):
        _completed_run(conn, created_at=NOW - timedelta(days=offset + 1))

    assert assessments.ensure_due(conn, policy=_policy(enabled=False), now=NOW) is None
    assert conn.execute("SELECT COUNT(*) FROM assessment_cycles").fetchone()[0] == 0


def test_complete_and_align_persist_bounded_actions_and_memory(ppy_home) -> None:
    conn = init_db()
    cycle = assessments.ensure_due(conn, policy=_policy(), now=NOW, force=True)
    assert cycle is not None

    completed = assessments.complete(
        cycle["id"],
        summary="Delivery is steady, but task routing needs tighter feedback loops.",
        strengths=["Kept workers within their model ceilings"],
        weaknesses=["Detected rework too late"],
        actions=[
            {
                "description": "Check worker progress after its first milestone.",
                "category": "practice",
                "observation": "Late review created avoidable rework",
                "likely_cause": "The manager waited for task completion",
                "baseline": "One check at task completion",
                "target": "One early check on every multi-step task",
                "measurement": "Share of multi-step tasks checked before completion",
            },
            {
                "description": "Change the configured worker ceiling.",
                "category": "config",
                "observation": "Easy work used a larger worker than needed",
                "likely_cause": "The current ceiling became the routing default",
                "baseline": "Current ceiling",
                "target": "A lower ceiling",
                "measurement": "Average worker tokens per comparable task",
            },
        ],
        conn=conn,
        policy=_policy(),
    )

    assert completed["status"] == "awaiting_user"
    assert [action["status"] for action in completed["actions"]] == ["proposed", "proposed"]
    assert completed["actions"][0]["requires_approval"] == 0
    assert completed["actions"][1]["requires_approval"] == 1
    assert completed["actions"][0]["measurement"].startswith("Share of")

    aligned = assessments.align(
        cycle["id"], decision="approved", notes="Try this for the next five runs.", conn=conn
    )

    assert aligned["status"] == "aligned"
    assert [action["status"] for action in aligned["actions"]] == ["active", "active"]
    assert json.loads(aligned["user_response"]) == {
        "decision": "approved",
        "notes": "Try this for the next five runs.",
    }
    history = memory.improvements_path().read_text(encoding="utf-8")
    assert "Assessment 1" in history
    assert "Try this for the next five runs." in history
    assert "Check worker progress after its first milestone." in history
