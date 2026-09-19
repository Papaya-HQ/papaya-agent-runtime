"""`ppy workers`: every in-flight worker, what it serves, and what it just did.

Read from a state database seeded by hand: a ticket taken with its display id and a
worker under it that is busy, a ticket taken before display ids were recorded with a
quiet worker, and a worker dispatched directly with no ticket at all.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from papaya_agent_runtime import cli, gate, papaya_events, serve, standalone, team, tracker
from papaya_agent_runtime.state import init_db, store

KEY = "PAP-231"
TITLE = "Add the /things endpoint"
OLD_ITEM = "0fc1596e-07af-435d-825e-658282066a82"
OLD_TITLE = "Desktop push shows an older message"
COMMAND = "uv run pytest tests/test_things.py -q"
NOTE = "Writing the endpoint handler."
QUIET_NOTE = "Reading the push code."
ESCAPE = re.compile(r"\x1b\[")


def _event(conn: Any, task_id: int, kind: str, payload: dict[str, Any], at: datetime) -> int:
    task = store.get_task(conn, task_id)
    event_id = store.append_event(
        conn,
        kind=kind,
        payload={"task_id": task_id, **payload},
        run_id=task["run_id"],
        task_id=task_id,
    )
    conn.execute("UPDATE events SET created_at = ? WHERE id = ?", (at.isoformat(), event_id))
    conn.commit()
    return event_id


def _backdate(conn: Any, task_id: int, at: datetime) -> None:
    conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (at.isoformat(), task_id))
    conn.commit()


def _ticket(conn: Any, item_id: str, title: str, event_payload: dict[str, Any]) -> tuple[int, int]:
    run_id = store.create_run(conn, title)
    ticket = store.add_task(conn, run_id=run_id, title=title)
    event = papaya_events.PapayaEvent(
        id=f"evt-{item_id[:4]}",
        kind="work_item.assigned",
        subject=f"work_item:{item_id}",
        payload=event_payload,
        work_item_id=item_id,
    )
    papaya_events.record_task(conn, ticket, event)
    serve.record_phase(conn, ticket, serve.PHASE_DISPATCHED, "dispatched")
    return run_id, ticket


def _worker(conn: Any, run_id: int, title: str, started: datetime) -> int:
    worker = store.add_task(conn, run_id=run_id, title=title, provider="claude")
    store.set_task_status(conn, worker, "in_progress")
    store.register_runner(
        conn, runner_id=f"runner-{worker}", task_id=worker, provider="claude", pid=os.getpid()
    )
    _backdate(conn, worker, started)
    return worker


def _bash(tool_id: str, command: str) -> dict[str, Any]:
    return {
        "message": {
            "content": [
                {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": command}}
            ]
        }
    }


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def world(ppy_home, now) -> dict[str, int]:
    """A busy worker on a new ticket, a quiet one on an old ticket, one with no ticket."""
    conn = init_db()
    try:
        # A ticket taken after this change: the daemon recorded its display id.
        run_id, ticket = _ticket(
            conn, "item-231", TITLE, {"work_item": {"short_id": KEY, "title": TITLE}}
        )
        papaya_events.record_work_item_label(
            conn,
            ticket,
            papaya_events.PapayaEvent(
                id="x",
                kind="k",
                subject="s",
                payload={"work_item": {"short_id": KEY, "title": TITLE}},
                work_item_id="item-231",
            ),
        )
        busy = _worker(conn, run_id, TITLE, now - timedelta(minutes=14))
        at = now - timedelta(minutes=12)
        step = timedelta(minutes=1)
        _event(conn, busy, "worker_progress", {"phase": "plan", "note": "Plan: add route."}, at)
        _event(conn, busy, "worker_assistant", _bash("t1", "git status"), at + step)
        _event(
            conn,
            busy,
            "worker_assistant",
            {"message": {"content": [{"type": "text", "text": "The route is in; now the tests."}]}},
            at + 2 * step,
        )
        _event(conn, busy, "worker_assistant", _bash("t2", COMMAND), at + 3 * step)
        for beat in range(3):  # a 30-second heartbeat on the call already said: no action
            _event(
                conn,
                busy,
                "worker_tool_progress",
                {"tool_use_id": "t2", "tool_name": "Bash", "elapsed_time_seconds": 30 * (beat + 1)},
                at + 3 * step + timedelta(seconds=30 * (beat + 1)),
            )
        _event(conn, busy, "worker_progress", {"phase": "implement", "note": NOTE}, at + 5 * step)
        _event(conn, busy, "worker_system", {"subtype": "init"}, at + 5 * step)  # chatter
        _event(conn, busy, "steer", {"message": "Skip pagination", "by": "person"}, at + 6 * step)
        _event(
            conn,
            ticket,
            serve.CHECKIN_EVENT,
            {"worker_task_id": busy, "trigger": "midpoint", "decision": "continue"},
            at + 7 * step,
        )
        _event(
            conn,
            busy,
            gate.GATE_QUEUED,
            {"key": f"task:{busy}:local:abc", "full": False, "reason": "behind task 9's gate"},
            at + 8 * step,
        )
        _event(
            conn, busy, gate.GATE_STARTED, {"full": False, "command": "make verify"}, at + 9 * step
        )
        _event(
            conn,
            busy,
            gate.GATE_RESULT,
            {"full": False, "exit_code": 0, "duration_seconds": 42.5},
            at + 10 * step,
        )
        _event(
            conn,
            busy,
            "resumed",
            {"message": "Carry on with the tests", "by": "manager"},
            at + 11 * step,
        )

        # A ticket taken before this change: no display id recorded, and its worker is quiet.
        old_run, old_ticket = _ticket(conn, OLD_ITEM, OLD_TITLE, {})
        quiet = _worker(conn, old_run, OLD_TITLE, now - timedelta(hours=2))
        long_ago = now - timedelta(minutes=50)
        second = timedelta(seconds=1)
        _event(
            conn,
            quiet,
            "worker_progress",
            {"phase": "plan", "note": QUIET_NOTE},
            long_ago - 3 * second,
        )
        _event(conn, quiet, "worker_assistant", _bash("q1", "rg push"), long_ago - 2 * second)
        _event(
            conn,
            quiet,
            gate.GATE_RESULT,
            {"full": True, "exit_code": 1, "duration_seconds": 300, "summary": "2 failed"},
            long_ago - second,
        )
        _event(conn, quiet, gate.GATE_KILLED, {"full": True}, long_ago)

        # Dispatched directly from a session: no ticket, no tracker record.
        loose_run = store.create_run(conn, "Tidy the logs")
        loose = _worker(conn, loose_run, "Tidy the logs", now - timedelta(minutes=3))
        _event(
            conn,
            loose,
            "worker_item.started",
            {"item": {"type": "command_execution", "command": "ls logs"}},
            now - timedelta(minutes=2),
        )
        _event(
            conn,
            loose,
            "worker_item.completed",
            {"item": {"type": "command_execution", "command": "ls logs", "exit_code": 0}},
            now - timedelta(minutes=2) + timedelta(seconds=5),
        )
        _event(
            conn,
            loose,
            "worker_item.completed",
            {"item": {"type": "agent_message", "text": "Logs tidied."}},
            now - timedelta(minutes=1),
        )
    finally:
        conn.close()
    return {
        "ticket": ticket,
        "busy": busy,
        "old_ticket": old_ticket,
        "quiet": quiet,
        "loose": loose,
    }


def _blocks(lines: list[str]) -> list[list[str]]:
    blocks: list[list[str]] = [[]]
    for line in lines:
        if line == "":
            blocks.append([])
        else:
            blocks[-1].append(line)
    return blocks


def test_every_in_flight_worker_prints_one_block_with_item_health_doing_gate_and_note(
    world, now
) -> None:
    found = team.workers(now=now)
    lines = team.render_workers(found, width=200)
    busy, quiet, loose = _blocks(lines)

    assert busy[0] == (f'{KEY} "{TITLE}" · task {world["busy"]} · in_progress 14m · alive')
    assert busy[1] == f"  doing: running `{COMMAND}` (1m in)"
    assert busy[2] == f"  note: [implement] {NOTE} (7m ago)"
    assert busy[3] == '  steered by a person: "Skip pagination"'
    assert busy[4] == "  last 5 actions:"

    # An older ticket has no display id recorded: its work item id stands in.
    assert quiet[0] == (
        f'work item {OLD_ITEM[:8]} "{OLD_TITLE}" · task {world["quiet"]} · in_progress 2h00m'
        " · quiet, silent 50m"
    )
    assert quiet[1] == "  doing: running `rg push`"
    assert quiet[2] == f"  note: [plan] {QUIET_NOTE} (50m ago)"

    assert loose[0] == f"no ticket · task {world['loose']} · in_progress 3m · alive"
    assert loose[1] == "  doing: running `ls logs`"
    assert [w["work_item"] for w in found][2] is None
    assert found[0]["work_item"]["source"] == "ticket"
    assert found[1]["work_item"]["source"] == "work item id"


def test_a_running_gate_is_said_on_its_own_line(world, now) -> None:
    conn = init_db()
    try:
        _event(
            conn,
            world["busy"],
            gate.GATE_STARTED,
            {"key": f"task:{world['busy']}:full:def", "full": True, "command": "make verify"},
            now - timedelta(seconds=40),
        )
    finally:
        conn.close()
    (busy, *_rest) = _blocks(team.render_workers(team.workers(now=now), width=200))
    assert "  gate: full suite running under the supervisor (40s in)" in busy


def test_actions_are_the_newest_n_newest_last_each_with_a_utc_time_and_an_age(world, now) -> None:
    found = team.workers(now=now, actions=3)
    busy, quiet, loose = _blocks(team.render_workers(found, width=200))

    def row(minutes_ago: int, age: str, words: str) -> str:
        clock = (now - timedelta(minutes=minutes_ago)).strftime("%H:%M:%S")
        return f"    {clock} UTC  {f'({age} ago)':<12}  {words}"

    assert busy[-4] == "  last 3 actions:"
    assert busy[-3:] == [
        row(3, "3m", "local gate started `make verify`"),
        row(2, "2m", "local gate passed in 42s"),
        row(1, "60s", "resumed by manager: Carry on with the tests"),
    ]
    # Fewer than asked: what it has.
    assert quiet[-4] == "  last 3 actions:"
    assert len([line for line in loose if line.startswith("    ")]) == 3


def test_the_words_start_in_one_column_whatever_the_age(world, now) -> None:
    lines = team.render_workers(team.workers(now=now, actions=20), width=200)
    rows = [line for line in lines if line.startswith("    ")]
    # 4 indent + `HH:MM:SS UTC` + 2 + a 12-wide age + 2: the words begin at column 32.
    assert rows and all(line[31] == " " and line[32] != " " for line in rows)


def test_each_action_kind_renders_once_in_words_with_no_raw_payload(world, now) -> None:
    found = team.workers(now=now, actions=20)
    said = {w["task_id"]: [a["what"] for a in w["actions"]] for w in found}

    assert said[world["busy"]] == [
        "note [plan] Plan: add route.",
        "ran `git status`",
        'said "The route is in; now the tests."',
        f"ran `{COMMAND}`",
        f"note [implement] {NOTE}",
        "steered by person: Skip pagination",
        f"check-in on worker task {world['busy']} (midpoint): continue",
        "local gate queued: behind task 9's gate",
        "local gate started `make verify`",
        "local gate passed in 42s",
        "resumed by manager: Carry on with the tests",
    ]
    assert said[world["quiet"]] == [
        f"note [plan] {QUIET_NOTE}",
        "ran `rg push`",
        "full suite failed (exit 1) in 5m: 2 failed",
        "full suite killed",
    ]
    assert said[world["loose"]] == [
        "ran `ls logs`",
        "finished `ls logs` (exit 0)",
        'said "Logs tidied."',
    ]
    tones = [a["tone"] for w in found for a in w["actions"] if a["tone"]]
    assert tones == ["pass", "fail", "fail"]
    for words in said.values():
        assert not any("{" in w or "tool_use" in w for w in words)


def test_a_tool_call_seen_only_through_its_heartbeat_is_one_action(world, now) -> None:
    conn = init_db()
    try:
        for beat in range(2):
            _event(
                conn,
                world["loose"],
                "worker_tool_progress",
                {
                    "tool_use_id": f"z9-heartbeat-{beat}",
                    "parent_tool_use_id": "z9",
                    "tool_name": "Bash",
                },
                now - timedelta(seconds=20 - beat),
            )
    finally:
        conn.close()
    (loose,) = [w for w in team.workers(now=now, actions=20) if w["task_id"] == world["loose"]]
    assert [a["what"] for a in loose["actions"]][-1] == "running Bash"
    assert [a["what"] for a in loose["actions"]].count("running Bash") == 1


def test_a_worker_with_no_ticket_says_so_and_still_lists_its_actions(world, capsys) -> None:
    assert cli.main(["workers", "--actions", "20"]) == 0
    loose = _blocks(capsys.readouterr().out.splitlines())[2]
    assert loose[0].startswith("no ticket · task ")
    assert loose[-1].endswith('said "Logs tidied."')


def test_a_tracked_worker_with_no_ticket_is_named_by_its_tracker_record(world, now) -> None:
    conn = init_db()
    try:
        tracker.link_task(conn, world["loose"], record="PAP-247", title="Tidy the logs")
    finally:
        conn.close()
    (loose,) = [w for w in team.workers(now=now) if w["task_id"] == world["loose"]]
    assert loose["work_item"] == {
        "key": "PAP-247",
        "title": "Tidy the logs",
        "source": "tracker",
        "ticket_task_id": None,
    }
    header = _blocks(team.render_workers([loose], width=200))[0][0]
    assert header.startswith('PAP-247 "Tidy the logs" · task ')


def test_json_carries_iso_timestamps_and_the_same_fields(world, capsys) -> None:
    assert cli.main(["workers", "--json", "--actions", "2"]) == 0
    out = capsys.readouterr().out
    assert not ESCAPE.search(out)
    answer = json.loads(out)
    assert answer["attention"] == {
        "paused": [],
        "gave_up": [],
        "repeating": [],
        "parked": [],
        "grown": [],
    }
    busy, quiet, loose = answer["workers"]
    assert busy["work_item"]["key"] == KEY and busy["work_item"]["title"] == TITLE
    assert (busy["status"], busy["session"], busy["note"]) == ("in_progress", "alive", NOTE)
    assert quiet["session"] == "quiet" and quiet["silent_seconds"] >= 50 * 60
    assert loose["work_item"] is None
    assert len(busy["actions"]) == 2
    for stamp in (busy["running_since"], busy["note_at"], *(a["at"] for a in busy["actions"])):
        assert datetime.fromisoformat(stamp).tzinfo is not None
    assert busy["actions"][-1]["what"] == "resumed by manager: Carry on with the tests"


def test_follow_reprints_when_a_worker_changes_and_stops_after_its_polls(
    world, capsys, monkeypatch
) -> None:
    def arrive(_seconds: float) -> None:
        conn = init_db()
        try:
            _event(
                conn,
                world["busy"],
                "worker_progress",
                {"phase": "test", "note": "Tests green."},
                datetime.now(UTC),
            )
        finally:
            conn.close()

    monkeypatch.setattr(team.time, "sleep", arrive)
    assert cli.main(["workers", "--follow", "--polls", "2", "--color", "never"]) == 0
    out = capsys.readouterr().out
    assert out.count("workers at ") == 3  # the first print, and one per change
    assert out.count("Tests green.") >= 2

    printed: list[int] = []
    unchanged = team.follow_workers(
        lambda found: printed.append(len(found)), follow=True, sleep=lambda _s: None, polls=2
    )
    assert unchanged == 1 and printed == [3]


def test_no_worker_in_flight_is_one_plain_line(ppy_home, capsys) -> None:
    assert cli.main(["workers"]) == 0
    assert capsys.readouterr().out == "no workers in flight\n"
    init_db().close()
    assert cli.main(["workers"]) == 0
    assert capsys.readouterr().out == "no workers in flight\n"


def test_actions_outside_one_to_twenty_is_refused(ppy_home, capsys) -> None:
    assert cli.main(["workers", "--actions", "21"]) == 2
    assert "1 to 20" in capsys.readouterr().err


# ── readability ─────────────────────────────────────────────────────────────


def test_nothing_is_coloured_when_stdout_is_not_a_terminal(world, capsys, monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert cli.main(["workers"]) == 0
    out = capsys.readouterr().out
    assert KEY in out and not ESCAPE.search(out)


def test_color_always_uses_exactly_the_named_styles(world, capsys, monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")  # an explicit --color always still colours
    assert cli.main(["workers", "--color", "always", "--actions", "20"]) == 0
    out = capsys.readouterr().out
    busy, quiet, loose = _blocks(out.splitlines())

    bold, dim, red, green, yellow, reset = (
        "\x1b[1m",
        "\x1b[2m",
        "\x1b[31m",
        "\x1b[32m",
        "\x1b[33m",
        "\x1b[0m",
    )
    assert busy[0] == (
        f'{bold}{KEY}{reset} {bold}"{TITLE}"{reset} · task {world["busy"]} · in_progress 14m'
        f" · {green}alive{reset}"
    )
    assert quiet[0].endswith(f" · {yellow}quiet, silent 50m{reset}")
    assert loose[0].startswith("no ticket · ")  # no ticket: nothing bold
    passed = next(line for line in busy if "local gate passed" in line)
    assert passed.endswith(f"{green}local gate passed in 42s{reset}")
    assert passed.startswith(f"    {dim}") and f"UTC{reset}  {dim}(" in passed
    failed = next(line for line in quiet if "full suite failed" in line)
    assert f"{red}full suite failed (exit 1) in 5m: 2 failed{reset}" in failed
    # The current action is default weight; nothing else carries a code.
    assert busy[1] == f"  doing: running `{COMMAND}` (1m in)"
    assert set(re.findall(r"\x1b\[[0-9;]*m", out)) == {bold, dim, red, green, yellow, reset}
    dead = team.Paint(True)(team._verdict_words({"session": "dead"}), "red")
    assert dead == f"{red}dead{reset}"


@pytest.mark.parametrize(
    ("choice", "env", "tty", "wanted"),
    [
        ("auto", {}, True, True),
        ("auto", {}, False, False),
        ("auto", {"NO_COLOR": "1"}, True, False),
        ("auto", {"CLICOLOR": "0"}, True, False),
        ("auto", {"TERM": "dumb"}, True, False),
        ("always", {"NO_COLOR": "1"}, False, True),
        ("never", {}, True, False),
    ],
)
def test_colour_is_auto_only_for_a_terminal_and_honours_no_color_and_clicolor(
    choice, env, tty, wanted
) -> None:
    class Stream:
        def isatty(self) -> bool:
            return tty

    assert team.colour_wanted(choice, Stream(), env) is wanted


def test_long_lines_are_clipped_to_the_width_at_a_word_boundary(world, now) -> None:
    conn = init_db()
    try:
        long = "make verify " + " ".join(f"--flag-number-{n}" for n in range(30))
        _event(conn, world["loose"], "worker_assistant", _bash("l1", long), now)
    finally:
        conn.close()
    lines = team.render_workers(team.workers(now=now), width=60, paint=team.Paint(True))
    plain = [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in lines]
    assert all(len(line) <= 60 for line in plain)
    clipped = [line for line in plain if line.endswith("…")]
    assert clipped
    for line in clipped:
        # Cut at a space: the word before the ellipsis is whole.
        assert re.search(r"(^|\s)[^\s]+…$", line) and not line.endswith(" …")
        stem = line[:-1].split()[-1]
        assert stem in long or stem in line
    header = plain[0]
    assert header.endswith("· alive") and len(header) <= 60  # the title gave way, not the facts


def test_the_width_is_100_when_stdout_is_not_a_terminal() -> None:
    class Pipe:
        def isatty(self) -> bool:
            return False

    assert team.terminal_width(Pipe()) == 100


# ── status --team agrees ────────────────────────────────────────────────────


def test_status_team_names_the_work_item_on_the_worker_line(world, capsys, monkeypatch) -> None:
    monkeypatch.setenv(standalone.QUIET_ENV, "1")
    assert cli.main(["status", "--team"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    busy = next(line for line in lines if f"worker task {world['busy']} " in line)
    quiet = next(line for line in lines if f"worker task {world['quiet']} " in line)
    assert busy.startswith(f"  worker task {world['busy']} · {KEY} · ")
    assert TITLE not in busy
    assert f" · work item {OLD_ITEM[:8]} · " in quiet
    assert not ESCAPE.search(out)


# ── the one write: the daemon records the display id when it takes a ticket ─


def test_a_ticket_taken_now_records_its_display_id_and_title_once(ppy_home) -> None:
    conn = init_db()
    try:
        run_id = store.create_run(conn, TITLE)
        ticket = store.add_task(conn, run_id=run_id, title=TITLE)
        taken = papaya_events.PapayaEvent(
            id="1",
            kind="work_item.assigned",
            subject="work_item:item-231",
            payload={"work_item": {"short_id": KEY, "title": TITLE}},
            work_item_id="item-231",
        )
        papaya_events.record_work_item_label(conn, ticket, taken)
        renamed = papaya_events.PapayaEvent(
            id="2",
            kind="work_item.assigned",
            subject="work_item:item-231",
            payload={"work_item": {"short_id": KEY, "title": "Renamed later"}},
            work_item_id="item-231",
        )
        papaya_events.record_work_item_label(conn, ticket, renamed)
        assert store.get_task_env(conn, ticket, papaya_events.WORK_ITEM_KEY) == KEY
        assert store.get_task_env(conn, ticket, papaya_events.WORK_ITEM_TITLE) == TITLE
        # An event summary names it `display_id`; either is read.
        assert papaya_events.work_item_label(
            papaya_events.PapayaEvent(
                id="3", kind="k", subject="s", payload={"work_item": {"display_id": "PAP-9"}}
            )
        ) == ("PAP-9", "")
    finally:
        conn.close()


def test_serve_records_the_display_id_when_it_takes_a_ticket(ppy_home) -> None:
    runner = serve.TicketRunner()
    event = papaya_events.PapayaEvent(
        id="evt-9",
        kind="work_item.assigned",
        subject="work_item:item-9",
        payload={"work_item": {"short_id": "PAP-9", "title": "Nine"}},
        work_item_id="item-9",
    )
    conn = init_db()
    try:
        held = runner._record(conn, event, None)
        assert store.get_task_env(conn, held.task_id, papaya_events.WORK_ITEM_KEY) == "PAP-9"
        assert store.get_task_env(conn, held.task_id, papaya_events.WORK_ITEM_TITLE) == "Nine"
    finally:
        conn.close()


def test_the_view_writes_nothing(world, now) -> None:
    conn = init_db()
    try:
        before = conn.total_changes, conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()
    team.render_workers(team.workers(now=now, actions=20))
    conn = init_db()
    try:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before[1]
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM task_env WHERE key LIKE 'papaya_work_item_%'"
            ).fetchone()[0]
            == 2
        )
    finally:
        conn.close()
