"""Hermetic tests for doctor capability-drift/schema output and usage reports."""

from __future__ import annotations

from papaya_agent_runtime.setup import doctor
from papaya_agent_runtime.state import init_db, store


def test_doctor_reports_schema_and_capability_drift(ppy_home) -> None:
    init_db()  # create the state DB at the current schema version
    data = doctor.collect()

    # Schema section reflects the current version.
    from papaya_agent_runtime.state.db import SCHEMA_VERSION

    assert data["state_db"]["expected"] == SCHEMA_VERSION
    assert data["state_db"].get("version") == SCHEMA_VERSION

    # Capability drift lists each provider that has a recorded probe version.
    providers = {d["provider"] for d in data["capability_drift"]}
    assert {"claude", "codex"} <= providers

    text = doctor.render_text(data)
    assert "State DB schema" in text
    assert "Provider capability matrix" in text


def test_usage_by_profile(ppy_home) -> None:
    conn = init_db()
    run_id = store.create_run(conn, "obj")
    task_id = store.add_task(conn, run_id=run_id, title="t")
    store.record_usage(
        conn,
        run_id=run_id,
        task_id=task_id,
        provider="claude",
        model="haiku",
        reasoning="low",
        input_tokens=100,
        output_tokens=20,
    )
    store.record_usage(
        conn,
        run_id=run_id,
        task_id=task_id,
        provider="claude",
        model="haiku",
        reasoning="low",
        input_tokens=50,
        output_tokens=10,
    )
    store.record_usage(
        conn,
        run_id=run_id,
        task_id=task_id,
        provider="codex",
        model=None,
        reasoning=None,
        input_tokens=200,
        output_tokens=40,
    )
    rows = store.usage_by_profile(conn, run_id)
    claude = next(r for r in rows if r["provider"] == "claude")
    assert claude["input_tokens"] == 150
    assert claude["output_tokens"] == 30
    assert claude["calls"] == 2

    totals = store.usage_totals(conn, run_id)
    assert totals["input_tokens"] == 350
    assert totals["output_tokens"] == 70
