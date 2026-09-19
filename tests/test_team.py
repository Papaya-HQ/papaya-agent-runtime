"""Copiloting the team from a session: `ppy status --team`, `ppy tail`, the status line.

Everything is read from a state database seeded by hand with one of each thing the
daemon writes: a held ticket and its worker, a delivered pull request in the reconcile
queue, a blocker, a round summary and a question waiting on a person.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from papaya_agent_runtime import (
    blockers,
    cli,
    deficiencies,
    papaya_events,
    reconcile,
    serve,
    standalone,
    sweep,
    team,
)
from papaya_agent_runtime.state import init_db, store

WORK_ITEM = "item-219"
TITLE = "Add the /things endpoint"
COMMAND = "uv run pytest tests/test_things.py -q"
NOTE = "Writing the endpoint handler."
PR_URL = "https://github.com/acme/api/pull/42"
QUESTION = "Should /things paginate?"
ROUND_LINE = "round: checking in on worker task 2 (still planning)"
PARKED_REASON = "the fix is on staging; waiting on QA's recheck"


def _event(conn: Any, task_id: int, kind: str, payload: dict[str, Any]) -> int:
    task = store.get_task(conn, task_id)
    return store.append_event(
        conn,
        kind=kind,
        payload={"task_id": task_id, **payload},
        run_id=task["run_id"],
        task_id=task_id,
    )


@pytest.fixture
def world(ppy_home) -> dict[str, int]:
    """One of everything `status --team` has a section for."""
    conn = init_db()
    try:
        run_id = store.create_run(conn, TITLE)
        ticket = store.add_task(conn, run_id=run_id, title=TITLE)
        papaya_events.record_task(
            conn,
            ticket,
            papaya_events.PapayaEvent(
                id="evt-1",
                kind="work_item.assigned",
                subject=f"work_item:{WORK_ITEM}",
                payload={},
                work_item_id=WORK_ITEM,
            ),
        )
        serve.record_phase(conn, ticket, serve.PHASE_PICKED_UP)
        serve.record_phase(conn, ticket, serve.PHASE_DISPATCHED, "Worker task 2 dispatched.")

        worker = store.add_task(conn, run_id=run_id, title=TITLE, provider="claude")
        store.set_task_status(conn, worker, "in_progress")
        store.update_task_fields(conn, worker, branch="ppy/task-2")
        store.register_runner(
            conn, runner_id="runner-2", task_id=worker, provider="claude", pid=os.getpid()
        )
        _event(conn, worker, "worker_progress", {"phase": "implement", "note": NOTE})
        _event(
            conn,
            worker,
            "worker_assistant",
            {
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Bash",
                            "input": {"command": COMMAND},
                        }
                    ]
                }
            },
        )
        store.add_todo(conn, QUESTION, run_id=run_id, task_id=ticket, blocked_on="user")

        delivered_run = store.create_run(conn, "Rename the flag")
        delivered = store.add_task(conn, run_id=delivered_run, title="Rename the flag")
        store.set_task_status(conn, delivered, "delivered")
        _event(conn, delivered, "delivered", {"pr_url": PR_URL})
        _event(
            conn,
            delivered,
            team.PR_OBSERVED_EVENT,
            {"pr": 42, "url": PR_URL, "state": "OPEN", "ci": "fail", "review": "CHANGES_REQUESTED"},
        )
        reconcile._append(conn, delivered, reconcile.QUEUED, {"fingerprint": "fp1", "rank": 2})

        store.append_event(conn, kind=team.ROUND_SUMMARY_EVENT, payload={"line": ROUND_LINE})
    finally:
        conn.close()

    now = datetime.now(UTC).isoformat()
    blockers.Ledger(
        open_={
            "fp": blockers.Blocker(
                fingerprint="fp",
                code="forge_unauthenticated",
                title="gh is signed out",
                steps=["gh auth login"],
                first_seen=now,
                last_seen=now,
            )
        }
    ).save()
    # A ticket a brief turn found nothing to build on, parked on a person.
    sweep.remember_parked("item-300", updated_at=now, reason=PARKED_REASON, label="PAP-300")
    return {"ticket": ticket, "worker": worker, "delivered": delivered}


def test_status_team_renders_every_section_from_a_state_with_one_of_each(
    world, capsys, monkeypatch
) -> None:
    # The suite is not connected; the standalone invitation is not a team line.
    monkeypatch.setenv(standalone.QUIET_ENV, "1")
    assert cli.main(["status", "--team"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()

    def under(heading: str) -> str:
        start = next(i for i, line in enumerate(lines) if line.startswith(heading))
        assert lines[start].endswith("(1):"), lines[start]
        return lines[start + 1]

    ticket = under("held tickets")
    assert f"ticket task {world['ticket']}" in ticket and f'"{TITLE}"' in ticket
    assert (
        "dispatched" in ticket and "held" in ticket and f"worker task {world['worker']}" in ticket
    )

    worker = under("workers")
    assert f"worker task {world['worker']}" in worker and "in_progress" in worker
    assert "session alive" in worker and f"running `{COMMAND}`" in worker
    assert f"note [implement] {NOTE}" in worker

    pr = under("pull requests")
    assert f"worker task {world['delivered']}" in pr and PR_URL in pr
    assert "OPEN, CI fail, review CHANGES_REQUESTED" in pr and "lane queued" in pr

    assert "reconcile lane: idle, 1 queued" in lines
    assert "forge_unauthenticated: gh is signed out" in under("blockers")
    assert any(line.startswith("last round (") and ROUND_LINE in line for line in lines)
    assert QUESTION in under("waiting on a person")
    parked = under("needs attention")
    assert "parked: PAP-300 waiting on a person since" in parked and PARKED_REASON in parked
    assert "un-parks when the item changes or a person comments after the stamp" in parked
    # One line per item, one heading per section, the delta since the last check (#72),
    # and nothing else.
    assert lines[-1].startswith("delta: ")
    assert len(lines) == 1 + 2 * 6 + 2 + 1


def test_status_team_json_carries_the_same_facts_machine_readably(world, capsys) -> None:
    assert cli.main(["status", "--team", "--json"]) == 0
    facts = json.loads(capsys.readouterr().out)

    assert set(facts) == {
        "at",
        "tickets",
        "workers",
        "pull_requests",
        "lane",
        "blockers",
        "last_round",
        "waiting_on_a_person",
        "attention",
    }
    assert [p["ticket"] for p in facts["attention"]["parked"]] == ["PAP-300"]
    (ticket,) = facts["tickets"]
    assert (ticket["task_id"], ticket["work_item_id"], ticket["phase"]) == (
        world["ticket"],
        WORK_ITEM,
        "dispatched",
    )
    assert ticket["worker_task_id"] == world["worker"]
    (worker,) = facts["workers"]
    assert worker["ticket_task_id"] == world["ticket"]
    assert (worker["tool"], worker["command"], worker["note"]) == ("Bash", COMMAND, NOTE)
    (pr,) = facts["pull_requests"]
    assert (pr["url"], pr["ci"], pr["review"], pr["lane"]) == (
        PR_URL,
        "fail",
        "CHANGES_REQUESTED",
        "queued",
    )
    assert facts["blockers"][0]["code"] == "forge_unauthenticated"
    assert facts["last_round"]["line"] == ROUND_LINE
    assert facts["waiting_on_a_person"][0]["text"] == QUESTION
    # `--json` alone means the team.
    assert cli.main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["tickets"][0]["task_id"] == world["ticket"]


def test_the_living_status_line_says_phase_worker_pull_request_and_the_wait(world) -> None:
    later = datetime.now(UTC) + timedelta(minutes=12)

    working = team.status_line(team.snapshot(now=later), world["ticket"])

    assert working == (
        f"Dispatched for 12m · worker working, running `{COMMAND}`; last note: {NOTE}"
        f" · waiting on you: {QUESTION}"
    )

    conn = init_db()
    try:
        # Its worker delivered, and a round saw the pull request go green and approved.
        _event(conn, world["worker"], "delivered", {"pr_url": PR_URL})
        _event(
            conn,
            world["worker"],
            team.PR_OBSERVED_EVENT,
            {"pr": 42, "url": PR_URL, "state": "OPEN", "ci": "pass", "review": "APPROVED"},
        )
        store.set_task_status(conn, world["worker"], "delivered")
    finally:
        conn.close()

    delivered = team.status_line(team.snapshot(now=later), world["ticket"])

    assert delivered == (
        f"Dispatched for 12m · PR {PR_URL}, CI pass, review approved · waiting on you: {QUESTION}"
    )
    # Only a held ticket carries one.
    assert team.status_line(team.snapshot(now=later), world["worker"]) is None


def test_the_status_line_is_written_in_place_only_when_it_changed_and_never_standalone(
    world, monkeypatch
) -> None:
    written: list[str] = []
    runner = serve.TicketRunner(status_comment=lambda _ticket, line: written.append(line) or True)
    ticket = serve.Ticket(held=serve.Held(world["ticket"], 1, None, None), job=None)

    # Standalone (the suite is not connected to Papaya): nothing is written, ever.
    assert asyncio.run(runner.keep_status_line(ticket)) is False
    assert written == []

    monkeypatch.setattr(standalone, "connected", lambda: True)

    async def twice() -> tuple[bool, bool]:
        return await runner.keep_status_line(ticket), await runner.keep_status_line(ticket)

    assert asyncio.run(twice()) == (True, False)
    assert len(written) == 1 and written[0].startswith("Dispatched for")
    # A local task with no work item behind it has nothing to write on, connected or not.
    local = serve.Ticket(held=serve.Held(world["delivered"], 2, None, None), job=None)
    assert asyncio.run(runner.keep_status_line(local)) is False
    assert len(written) == 1
    # With no editor (the default until backend #636), nothing is ever written.
    assert asyncio.run(serve.TicketRunner().keep_status_line(ticket)) is False


def test_tail_prints_the_events_since_in_order_one_line_each_and_follow_streams(
    world, ppy_home
) -> None:
    conn = init_db()
    try:
        old = _event(conn, world["ticket"], store.TICKET_PHASE_EVENT, {"phase": "briefing"})
        conn.execute(
            "UPDATE events SET created_at = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(hours=2)).isoformat(), old),
        )
        conn.commit()
        _event(conn, world["worker"], "worker_tool_progress", {"tool_name": "Bash"})
        _event(
            conn,
            world["ticket"],
            serve.CHECKIN_EVENT,
            {"worker_task_id": world["worker"], "trigger": "plan", "decision": "continue"},
        )
        _event(conn, world["worker"], "steer", {"message": "Skip pagination", "by": "person"})
    finally:
        conn.close()
    deficiencies.record(deficiencies.MISSED_TURN, "the brief turn ended without a dispatch")

    lines: list[str] = []

    def arrive(_seconds: float) -> None:
        conn = init_db()
        try:
            _event(
                conn, world["worker"], "worker_progress", {"phase": "test", "note": "Tests green."}
            )
        finally:
            conn.close()

    written = team.tail(
        lines.append, since=timedelta(minutes=10), follow=True, sleep=arrive, polls=1
    )

    said = [line.split(" ", 1)[1] for line in lines]
    assert written == len(lines)
    t, w, d = world["ticket"], world["worker"], world["delivered"]
    assert said == [
        f"task {t} phase picked_up",
        f"task {t} phase dispatched: Worker task 2 dispatched.",
        f"task {w} note [implement] {NOTE}",
        f"task {d} delivered: {PR_URL}",
        f"task {d} PR {PR_URL}: OPEN, CI fail, review CHANGES_REQUESTED",
        f"task {d} queued for the reconcile lane",
        ROUND_LINE,
        f"task {t} check-in on worker task {w} (plan): continue",
        f"task {w} steered by person: Skip pagination",
        said[9],
        f"task {w} note [test] Tests green.",
    ]
    assert said[9].startswith("deficiency missed-turn: ") and said[9].endswith("(seen 1x)")
    assert not any("briefing" in line for line in said)
    assert all("\n" not in line for line in lines)
