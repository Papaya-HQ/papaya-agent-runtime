"""Hermetic tests for durable scoped decisions."""

from __future__ import annotations

import pytest

from papaya_agent_runtime import decisions
from papaya_agent_runtime.state import init_db, store


def test_fingerprint_normalizes_whitespace_and_case() -> None:
    assert decisions.fingerprint("Which  endpoint?") == decisions.fingerprint("which endpoint?")
    assert decisions.fingerprint("a") != decisions.fingerprint("b")


def _run(conn) -> int:
    return store.create_run(conn, "obj")


def test_record_and_find_run_scoped(ppy_home) -> None:
    conn = init_db()
    run_id = _run(conn)
    decisions.record_decision(
        conn, question="Which endpoint?", answer="/v2", scope="run", run_id=run_id
    )
    hit = decisions.find_matching(conn, "which endpoint?", run_id=run_id)
    assert hit is not None
    assert hit.answer == "/v2"
    # Different run does not match a run-scoped decision.
    assert decisions.find_matching(conn, "which endpoint?", run_id=run_id + 1) is None


def test_global_beats_run(ppy_home) -> None:
    conn = init_db()
    run_id = _run(conn)
    decisions.record_decision(conn, question="License?", answer="MIT", scope="run", run_id=run_id)
    decisions.record_decision(conn, question="License?", answer="Apache-2.0", scope="global")
    hit = decisions.find_matching(conn, "license?", run_id=run_id)
    assert hit is not None
    assert hit.scope == "global"
    assert hit.answer == "Apache-2.0"


def test_supersede_hides_decision(ppy_home) -> None:
    conn = init_db()
    run_id = _run(conn)
    old = decisions.record_decision(conn, question="Q?", answer="old", scope="run", run_id=run_id)
    new = decisions.record_decision(conn, question="Q?", answer="new", scope="run", run_id=run_id)
    decisions.supersede(conn, old, new)
    hit = decisions.find_matching(conn, "Q?", run_id=run_id)
    assert hit is not None
    assert hit.answer == "new"


def test_stale_decision_when_premise_changed(ppy_home) -> None:
    conn = init_db()
    run_id = _run(conn)
    decisions.record_decision(
        conn,
        question="Which db?",
        answer="postgres",
        scope="run",
        run_id=run_id,
        context="repo@aaaaaaaaaaaa",
    )
    # Same context reuses silently.
    hit = decisions.find_matching(conn, "which db?", run_id=run_id, context="repo@aaaaaaaaaaaa")
    assert hit is not None and hit.answer == "postgres"
    # A changed premise raises rather than silently reusing.
    with pytest.raises(decisions.StaleDecision):
        decisions.find_matching(conn, "which db?", run_id=run_id, context="repo@bbbbbbbbbbbb")


def test_invalidate_and_forget(ppy_home) -> None:
    conn = init_db()
    run_id = _run(conn)
    did = decisions.record_decision(
        conn, question="Keep?", answer="yes", scope="run", run_id=run_id
    )
    decisions.invalidate(conn, did)
    assert decisions.find_matching(conn, "Keep?", run_id=run_id) is None
    assert any(d["id"] == did for d in decisions.list_decisions(conn, include_inactive=True))
    assert not any(d["id"] == did for d in decisions.list_decisions(conn))

    decisions.forget(conn, did)
    assert not any(d["id"] == did for d in decisions.list_decisions(conn, include_inactive=True))
