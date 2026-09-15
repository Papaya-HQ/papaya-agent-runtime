"""Worker reflections: filed by workers, durable in state, fed into the manager's review."""

from __future__ import annotations

import json

import pytest

from papaya_agent_runtime import assessments, memory, reflections
from papaya_agent_runtime.cli import main
from papaya_agent_runtime.state import init_db, store


@pytest.fixture
def ppy_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PPY_HOME", str(tmp_path / ".ppy"))
    return tmp_path / ".ppy"


def _task(conn, title="build the widget"):
    run_id = store.create_run(conn, "ship it")
    task_id = store.add_task(conn, run_id=run_id, title=title)
    store.set_task_status(conn, task_id, "worker_done")
    return run_id, task_id


def test_record_requires_a_real_task_and_some_text(ppy_home) -> None:
    conn = init_db()
    with pytest.raises(reflections.ReflectionError, match="not found"):
        reflections.record(999, self_note="x", conn=conn)
    _, task_id = _task(conn)
    with pytest.raises(reflections.ReflectionError, match="--self and/or --manager"):
        reflections.record(task_id, self_note="  ", manager_note="", conn=conn)
    with pytest.raises(reflections.ReflectionError, match="at most"):
        reflections.record(task_id, self_note="x" * 4001, conn=conn)


def test_reflections_are_durable_events_with_both_voices(ppy_home) -> None:
    conn = init_db()
    run_id, task_id = _task(conn)
    reflections.record(
        task_id,
        self_note="Tests-first worked; I lost 20 minutes to a stale test database.",
        manager_note="Brief was clear. The 'where to start' commit was wrong once.",
        conn=conn,
    )
    reflections.record(task_id, manager_note="Review turnaround was fast.", conn=conn)

    entries = reflections.history(task_id, conn=conn)
    assert [e["self"] for e in entries] == [
        "Tests-first worked; I lost 20 minutes to a stale test database.",
        "",
    ]
    assert entries[1]["manager"] == "Review turnaround was fast."
    assert all(e["run_id"] == run_id for e in entries)
    kinds = {r["kind"] for r in conn.execute("SELECT kind FROM events").fetchall()}
    assert "worker_reflection" in kinds

    text = reflections.render(entries, heading="# reflections")
    assert "self: Tests-first worked" in text and "manager: Review turnaround" in text
    assert reflections.render([]).strip() == "- no reflections filed"


def test_since_returns_titled_entries_in_window(ppy_home) -> None:
    conn = init_db()
    _, task_id = _task(conn, title="add the summary route")
    reflections.record(task_id, self_note="fine", conn=conn)
    entries = reflections.since("1970-01-01T00:00:00+00:00", conn=conn)
    assert len(entries) == 1 and entries[0]["title"] == "add the summary route"
    assert reflections.since("2999-01-01T00:00:00+00:00", conn=conn) == []


def test_evidence_packet_carries_reflections(ppy_home) -> None:
    conn = init_db()
    _, task_id = _task(conn, title="wire the routes")
    reflections.record(
        task_id,
        self_note="Would have gone faster with the auth dependency named in the brief.",
        manager_note="Steering came too late to change my start commit.",
        conn=conn,
    )
    evidence = assessments.collect_evidence(conn)
    assert evidence["reflections"]["count"] == 1
    entry = evidence["reflections"]["entries"][0]
    assert entry["task_id"] == task_id and entry["title"] == "wire the routes"
    assert "auth dependency" in entry["self"]
    assert "too late" in entry["manager"]
    # The manager's review instructions say to weigh them.
    prompt = assessments.assessment_prompt(
        {"id": 1, "trigger": "completed_runs", "evidence": evidence}
    )
    assert "reflections" in prompt and "assessments of you" in prompt


def test_workers_are_told_to_reflect(ppy_home) -> None:
    preamble = memory.worker_context("demo", task_id=7)
    assert "ppy reflect 7 --self" in preamble and "--manager" in preamble
    assert preamble.index("ppy reflect 7") < preamble.index("--phase done")

    review = memory.worker_context("demo", task_id=8, ends_at="review")
    assert "--phase review" in review
    assert "--phase done" not in review
    assert "do not call done" in review


def test_cli_reflect_records_and_shows(ppy_home, capsys) -> None:
    conn = init_db()
    _, task_id = _task(conn)

    assert main(["reflect", str(task_id), "--self", "learned x", "--manager", "brief ok"]) == 0
    assert "reflection recorded" in capsys.readouterr().out

    assert main(["reflect", str(task_id)]) == 0
    out = capsys.readouterr().out
    assert "self: learned x" in out and "manager: brief ok" in out

    assert main(["reflect", str(task_id), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["self"] == "learned x"

    assert main(["reflect", str(task_id), "--self", ""]) == 1
    assert "--self and/or --manager" in capsys.readouterr().err
