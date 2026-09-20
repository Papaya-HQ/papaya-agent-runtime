"""The runtime opens issues on itself: the ledger, the signals, and what leaves the machine.

`gh` is faked at the one seam `GhForge` has (a callable taking `gh`'s arguments and
stdin), so what is tested is the real command line the runtime would run. The clock
is a fake too: the daily cap is counted on its date.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import test_serve
from papaya_agent_runtime import cli, deficiencies, papaya, papaya_events, prompts, readiness, serve
from papaya_agent_runtime.setup import doctor
from papaya_agent_runtime.state import store
from papaya_agent_runtime.state.db import init_db
from test_serve import (
    EVENT,
    FakeEvents,
    FakePapaya,
    Harness,
    Turn,
    _assigned,
    _deliver,
    _review_ticket,
    _runner,
    _until,
)

# `serve`'s own fixtures, bound by name (see tests/test_rounds.py for why not imported).
globals().update(
    {name: getattr(test_serve, name) for name in ("client_home", "ready", "registered_repo")}
)

RUNTIME_REPO = "acme/papaya-agent-runtime"
LINE = "could not read the gate record for task 9"


@dataclass
class FakeGh:
    """`gh`, answering the issue and label calls the runtime makes, and recording them."""

    calls: list[tuple[list[str], str | None]] = field(default_factory=list)
    issues: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __call__(self, args: list[str], stdin: str | None = None) -> tuple[int, str, str]:
        self.calls.append((list(args), stdin))
        if args[:2] == ["label", "create"]:
            return 0, "", ""
        if args[:2] == ["issue", "create"]:
            url = f"https://github.com/{RUNTIME_REPO}/issues/{len(self.issues) + 1}"
            labels = [args[i + 1] for i, a in enumerate(args) if a == "--label"]
            title = args[args.index("--title") + 1]
            self.issues[url] = {
                "title": title,
                "body": stdin,
                "labels": labels,
                "state": "OPEN",
                "comments": [],
            }
            return 0, url + "\n", ""
        if args[:2] == ["issue", "view"]:
            return 0, json.dumps({"state": self.issues[args[2]]["state"]}), ""
        if args[:2] == ["issue", "comment"]:
            self.issues[args[2]]["comments"].append(stdin)
            return 0, "", ""
        if args[:2] == ["issue", "reopen"]:
            issue = self.issues[args[2]]
            issue["state"] = "OPEN"
            issue["comments"].append(args[args.index("--comment") + 1])
            return 0, "", ""
        if args[:2] == ["issue", "close"]:
            issue = self.issues[args[2]]
            issue["state"] = "CLOSED"
            issue["comments"].append(args[args.index("--comment") + 1])
            return 0, "", ""
        return 1, "", f"unexpected gh call {args}"

    def created(self) -> list[dict[str, Any]]:
        return list(self.issues.values())


class WallClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _reporter(gh: FakeGh, *, clock=None, **settings: Any) -> deficiencies.Reporter:
    return deficiencies.Reporter(
        forge=deficiencies.GhForge(run=gh),
        clock=clock,
        config=deficiencies.Settings(repo=RUNTIME_REPO, **settings),
    )


def _serve_with(harness: Harness, client_home, runner, reporter) -> int:
    async def scenario() -> int:
        task = asyncio.create_task(
            serve.run(
                serve.parse_args(["--working-directory", str(client_home.work_dir)]),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                extra=harness.extra(),
                runner=runner,
                self_report=reporter,
            )
        )
        await _until(lambda: harness.results, what="the ticket to end")
        harness.loop.request_stop()
        return await task

    return asyncio.run(scenario())


def _review_says_runtime(turn: Turn) -> str:
    _deliver(turn)
    return f"Approved and delivered.\nRUNTIME: {LINE}"


# ── the acceptance tests ────────────────────────────────────────────────────


def test_a_review_turn_ending_with_a_runtime_line_opens_one_issue(
    ppy_home, client_home, ready, registered_repo
) -> None:
    gh, papaya_api = FakeGh(), FakePapaya()
    turns = _review_ticket(_review_says_runtime, papaya_api)

    code = _serve_with(
        Harness(FakeEvents([EVENT])), client_home, _runner(turns, papaya_api), _reporter(gh)
    )

    assert code == 0
    (row,) = deficiencies.ledger(include_all=True)
    assert (row.kind, row.count, row.status) == (deficiencies.TURN_REPORT, 1, deficiencies.REPORTED)
    (issue,) = gh.created()
    assert row.issue_url == f"https://github.com/{RUNTIME_REPO}/issues/1"
    assert LINE in issue["title"]
    assert issue["labels"] == [deficiencies.LABEL, deficiencies.TURN_REPORT]
    body = issue["body"]
    for heading in ("## What happened", "## Evidence", "## What the runtime did instead"):
        assert heading in body
    assert LINE in body
    assert "ticket `item-9`" in body and "repo `runtime`" in body and "turn `review`" in body
    assert "review-1.log" in body
    # The ticket's title never leaves the machine.
    assert "Fix the thing" not in body and "Fix the thing" not in issue["title"]
    labels = [args for args, _ in gh.calls if args[:2] == ["label", "create"]]
    assert {args[2] for args in labels} == {deficiencies.LABEL, deficiencies.TURN_REPORT}


def test_the_same_line_on_a_second_ticket_comments_and_opens_nothing_new(
    ppy_home, client_home, ready, registered_repo
) -> None:
    gh = FakeGh()
    reporter = _reporter(gh)
    for event in (EVENT, _assigned(102, "item-10", "Another thing")):
        papaya_api = FakePapaya()
        turns = _review_ticket(_review_says_runtime, papaya_api)
        harness = Harness(FakeEvents([event]))
        assert _serve_with(harness, client_home, _runner(turns, papaya_api), reporter) == 0

    (row,) = deficiencies.ledger(include_all=True)
    assert row.count == 2 and row.reported_count == 2
    (issue,) = gh.created()
    (comment,) = issue["comments"]
    assert "2 time(s)" in comment and "ticket `item-10`" in comment
    assert "ticket `item-9`" not in comment


def test_a_stalled_hand_back_with_a_live_worker_session_is_recorded_without_a_turn(
    ppy_home, registered_repo
) -> None:
    from papaya_agent_client.listener import STOP_STALLED

    conn = init_db()
    run_id = store.create_run(conn, "ticket")
    ticket_id = store.add_task(conn, run_id=run_id, title="ticket")
    conn.close()
    worker_id = test_serve.dispatch_worker(run_id)
    conn = init_db()
    store.register_runner(
        conn, runner_id="runner-1", task_id=worker_id, provider="fake", pid=os.getpid()
    )
    conn.close()

    class Stop:
        reason = STOP_STALLED

        def is_set(self) -> bool:
            return True

    @dataclass
    class Job:
        stop: Any = field(default_factory=Stop)
        env: dict[str, str] = field(default_factory=dict)
        subject: str = "work_item:item-9"
        job_id: str = "job-1"

        def report_progress(self, phase: str, detail: str) -> None:
            pass

    event = papaya_events.PapayaEvent(
        id=101,
        kind="work_item.assigned",
        subject="work_item:item-9",
        payload={},
        work_item_id="item-9",
    )
    held = serve.Held(task_id=ticket_id, run_id=run_id, repo="runtime", event=event)
    turns = test_serve.FakeTurns()
    runner = _runner(turns, FakePapaya())

    result = asyncio.run(runner._stopped(serve.Ticket(held=held, job=Job())))

    assert result["exit_code"] == 0
    assert turns.calls == []
    (row,) = deficiencies.ledger()
    assert row.kind == deficiencies.STALL_WHILE_LIVE
    assert row.status == deficiencies.PENDING
    assert row.evidence[0]["worker_task_id"] == worker_id
    assert row.evidence[0]["ticket"] == "item-9"


def test_six_new_fingerprints_in_a_day_open_five_and_the_sixth_the_next_day(ppy_home) -> None:
    gh, clock = FakeGh(), WallClock()
    reporter = _reporter(gh, clock=clock)
    for word in ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot"):
        deficiencies.record(deficiencies.TURN_REPORT, f"the {word} fact was missing", clock=clock)

    reporter.flush()
    assert len(gh.created()) == 5
    clock.now += timedelta(hours=14)  # 23:00, the same day
    reporter.flush()
    assert len(gh.created()) == 5
    waiting = [d for d in deficiencies.ledger() if d.status == deficiencies.PENDING]
    assert [d.detail for d in waiting] == ["the foxtrot fact was missing"]

    clock.now += timedelta(hours=2)  # the next day
    reporter.flush()
    assert len(gh.created()) == 6
    assert deficiencies.summary() == {"recorded": 6, "open_issues": 6, "waiting": 0}


def test_nothing_private_reaches_the_issue(ppy_home, monkeypatch) -> None:
    home = str(Path.home())
    title = "Rework the billing export for Globex"
    description = "Globex wants the CSV to include their unreleased Q3 pricing tiers."
    person = "Dana Scully"
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    hunk = (
        "diff --git a/billing/export.py b/billing/export.py\n"
        "@@ -10,6 +10,7 @@ def export(rows):\n"
        "-    SECRET_MARGIN = 0.42\n"
        "+    SECRET_MARGIN = 0.37\n"
        "     return rows"
    )
    event = papaya_events.PapayaEvent(
        id=7,
        kind="work_item.assigned",
        subject="work_item:item-7",
        payload={
            "work_item": {
                "id": "item-7",
                "key": "PAP-7",
                "title": title,
                "description": description,
                "assignee": {"display_name": person, "email": "dana@example.com"},
            }
        },
        work_item_id="item-7",
    )
    error = (
        f"Traceback: {home}/code/billing/export.py failed for {person}\n"
        f"{hunk}\n"
        f"while quoting '{description}' with Authorization: Bearer {token}\n"
        f"RuntimeError: gate record missing for {title}"
    )

    deficiencies.record(
        deficiencies.TURN_REPORT,
        f"could not read the gate record for {title} as {person} ({token})",
        evidence={
            "ticket": serve.ticket_key(event),
            "repo": "billing",
            "transcript": f"{home}/.ppy/runs/3/turns/review-1.log",
            "error": error,
            # Fields a caller might pass that an issue must never carry.
            "title": title,
            "description": description,
            "comment": "Dana said the margin is confidential",
            "author_name": person,
        },
        scrub=serve.private_strings(event),
    )
    gh = FakeGh()
    _reporter(gh).flush()

    (issue,) = gh.created()
    sent = issue["title"] + "\n" + issue["body"]
    for forbidden in (
        title,
        "Globex",
        description,
        "unreleased Q3 pricing",
        person,
        "dana@example.com",
        token,
        "SECRET_MARGIN",
        "@@ -10,6",
        "diff --git",
        home,
        "confidential",
    ):
        assert forbidden not in sent, forbidden
    assert "ticket `PAP-7`" in sent
    assert "~/.ppy/runs/3/turns/review-1.log" in sent
    assert "~/code/billing/export.py" in sent
    # Nor does the ledger keep any of it.
    stored = init_db().execute("SELECT * FROM deficiencies").fetchone()
    for forbidden in (title, description, person, token, "SECRET_MARGIN", home):
        assert forbidden not in json.dumps(dict(stored)), forbidden


def test_self_reporting_switched_off_records_to_the_ledger_and_opens_nothing(ppy_home) -> None:
    gh = FakeGh()
    deficiencies.record(deficiencies.TURN_REPORT, LINE)

    assert _reporter(gh, enabled=False).flush() == []

    assert gh.calls == []
    (row,) = deficiencies.ledger()
    assert (row.status, row.issue_url) == (deficiencies.PENDING, None)


# ── the rest of the contract ────────────────────────────────────────────────


def test_a_closed_issue_that_recurs_is_reopened_with_a_comment_not_duplicated(ppy_home) -> None:
    gh = FakeGh()
    reporter = _reporter(gh)
    deficiencies.record(deficiencies.TURN_REPORT, LINE)
    reporter.flush()
    (url,) = gh.issues
    gh.issues[url]["state"] = "CLOSED"

    deficiencies.record(deficiencies.TURN_REPORT, LINE.replace("9", "12"))
    reporter.flush()

    assert len(gh.issues) == 1
    assert gh.issues[url]["state"] == "OPEN"
    assert len(gh.issues[url]["comments"]) == 1
    assert any(args[:2] == ["issue", "reopen"] for args, _ in gh.calls)


def _denials(command: str) -> list[dict[str, Any]]:
    """What `ClaudeAdapter.permission_denials` reads off a turn's `result` event."""
    return [{"tool_name": "Bash", "tool_input": {"command": command}}]


def test_a_plain_denial_outside_the_safe_family_is_due_an_issue_once_twice_on_one_repo(
    ppy_home,
) -> None:
    conn = init_db()
    run_id = store.create_run(conn, "run")
    tasks = {}
    for name in ("api", "web"):
        repo_id = store.add_repo(
            conn,
            name=name,
            origin=f"https://github.com/acme/{name}",
            local_path="/tmp/x",
            default_branch="main",
            base_sha=None,
        )
        tasks[name] = [
            store.add_task(conn, run_id=run_id, title="w", repo_id=repo_id) for _ in "ab"
        ]
    conn.close()

    # Inside the safe family: the runtime learns it (`tool_learning`), and reports nothing.
    for _ in range(2):
        deficiencies.record_denials(
            _denials("wc -l README.md"), task_id=tasks["api"][0], run_id=run_id, worktree=None
        )
    assert deficiencies.ledger(include_all=True) == []

    deficiencies.record_denials(
        _denials("terraform plan"),
        task_id=tasks["api"][0],
        run_id=run_id,
        worktree=None,
    )
    deficiencies.record_denials(
        _denials("terraform validate"),
        task_id=tasks["web"][0],
        run_id=run_id,
        worktree=None,
    )
    (row,) = deficiencies.ledger(include_all=True)
    assert (row.count, row.status) == (2, deficiencies.WATCHING)

    deficiencies.record_denials(
        _denials("terraform plan -out x"),
        task_id=tasks["api"][1],
        run_id=run_id,
        worktree=None,
    )
    (row,) = deficiencies.ledger()
    assert (row.kind, row.count, row.status) == (
        deficiencies.WORKER_DENIAL,
        3,
        deficiencies.PENDING,
    )
    assert row.evidence[-1]["pattern"] == "Bash(terraform:*)"
    assert row.evidence[-1]["command"] == "terraform plan -out x"


def test_a_check_in_steering_twice_for_one_reason_on_one_ticket_reaches_its_threshold(
    ppy_home,
) -> None:
    for ticket in ("PAP-1", "PAP-2", "PAP-1"):
        deficiencies.record(
            deficiencies.REPEATED_STEER,
            "quiet",
            scope=f"ticket:{ticket}",
            evidence={"ticket": ticket},
        )
    (row,) = deficiencies.ledger()
    assert (row.count, row.status) == (3, deficiencies.PENDING)


def test_a_non_github_origin_keeps_the_ledger_and_warns_once(ppy_home, caplog) -> None:
    gh = FakeGh()
    reporter = deficiencies.Reporter(
        forge=deficiencies.GhForge(run=gh),
        config=deficiencies.Settings(),
        origin=lambda: "git@gitlab.com:acme/runtime.git",
    )
    deficiencies.record(deficiencies.TURN_REPORT, LINE)

    reporter.flush()
    reporter.flush()

    assert gh.calls == []
    warnings = [r for r in caplog.records if "not a GitHub repository" in r.getMessage()]
    assert len(warnings) == 1


def test_every_turn_prompt_invites_the_runtime_line_and_the_runner_reads_it() -> None:
    for turn in prompts.TURNS:
        assert prompts.RUNTIME_RULE in prompts.load(turn), turn
    said = "Checked.\nRUNTIME: `ppy task show` has no gate record\nCHECK-IN: continue"
    assert serve.runtime_report(said) == "`ppy task show` has no gate record"
    assert serve.checkin_decision(said) == (prompts.CHECKIN_CONTINUE, "")
    assert serve.runtime_report("Delivered.") is None
    assert serve.runtime_report("say `RUNTIME: <what got in the way>`") is None


def test_fingerprints_ignore_what_varies_between_occurrences() -> None:
    first = deficiencies.fingerprint(
        deficiencies.TURN_REPORT, "no gate record for task 9 at abc1234"
    )
    again = deficiencies.fingerprint(
        deficiencies.TURN_REPORT, "No gate record for task 12 at 9f8e7d6c"
    )
    other = deficiencies.fingerprint(
        deficiencies.MISSED_TURN, "no gate record for task 9 at abc1234"
    )
    assert first == again != other


#: The two ledger details behind issues #40 and #49, verbatim: one cause, two sentences.
ISSUE_40 = (
    "propose_memory rejected an agent-scoped proposal for agent dfe335d3 (machine-extracted "
    "memory is only allowed on a personal agent), and its provenance field accepts no "
    "work-item source type, so the routing memory from step 6 could not be proposed."
)
ISSUE_49 = (
    "propose_memory refused an agent-scoped proposal for agent dfe335d3 because it is a "
    "shared agent, and it rejects `work_item` as a provenance source type. The dispatch "
    "step's instruction to propose a routing memory can't be followed for this identity."
)


def test_two_wordings_of_one_refusal_fingerprint_the_same_and_other_causes_do_not() -> None:
    first = deficiencies.fingerprint(deficiencies.TURN_REPORT, ISSUE_40)
    assert first == deficiencies.fingerprint(deficiencies.TURN_REPORT, ISSUE_49)
    # Issues #46 and #47 are other causes and stay apart from it and from each other.
    push = (
        "the check-in for task 18 said nothing had been pushed in 47 minutes, but the branch "
        "head on the remote is already fc0cbdcec"
    )
    gate = "gate run for task 19 shared the local test database with other in-flight tasks"
    others = {deficiencies.fingerprint(deficiencies.TURN_REPORT, line) for line in (push, gate)}
    assert len(others) == 2 and first not in others
    # The cause is the tool, the refusal and the repository, not the sentence around them.
    assert deficiencies.reduce_turn_report(ISSUE_49) == "propose_memory|agent-scop proposal|"
    # Naming an exception is stronger than naming a tool: the class and where it came
    # from lead, and the repository still tells two of them apart.
    in_api = deficiencies.reduce_turn_report("`ppy gate` raised TimeoutError in repo `api`")
    assert in_api == "TimeoutError|ppy gate|api"
    assert deficiencies.fingerprint(
        deficiencies.TURN_REPORT, "propose_memory refused an agent-scoped proposal in repo api"
    ) != deficiencies.fingerprint(
        deficiencies.TURN_REPORT, "propose_memory refused an agent-scoped proposal in repo web"
    )


def test_a_recurrence_worded_differently_climbs_the_count_and_comments_once(ppy_home) -> None:
    gh = FakeGh()
    reporter = _reporter(gh)
    deficiencies.record(deficiencies.TURN_REPORT, ISSUE_40)
    reporter.flush()
    deficiencies.record(deficiencies.TURN_REPORT, ISSUE_49)
    reporter.flush()

    (row,) = deficiencies.ledger()
    assert (row.count, row.reported_count) == (2, 2)
    (issue,) = gh.created()
    assert len(issue["comments"]) == 1


def _legacy_row(conn, key: str, detail: str, url: str | None, at: str) -> None:
    """A turn report as a build before reduced fingerprints wrote it."""
    title = f"{deficiencies.KINDS[deficiencies.TURN_REPORT].title}: {detail}"[:117] + "..."
    status = deficiencies.REPORTED if url else deficiencies.PENDING
    conn.execute(
        "INSERT INTO deficiencies (fingerprint, kind, title, detail, first_seen, last_seen, "
        "count, evidence, issue_url, status, opened_at, reported_count) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
        (
            key,
            deficiencies.TURN_REPORT,
            title,
            detail,
            at,
            at,
            json.dumps([{"at": at, "turn": "brief", "n": 1}]),
            url,
            status,
            at if url else None,
            1 if url else 0,
        ),
    )


def test_start_closes_a_later_duplicate_turn_report_with_a_comment_and_keeps_the_first(
    ppy_home,
) -> None:
    gh = FakeGh()
    first = gh(["issue", "create", "--title", "#40", "--label", "turn-report"], "body")[1].strip()
    second = gh(["issue", "create", "--title", "#49", "--label", "turn-report"], "body")[1].strip()
    other = gh(["issue", "create", "--title", "#47", "--label", "turn-report"], "body")[1].strip()
    gate = "gate run for task 19 shared the local test database with other in-flight tasks"
    conn = init_db()
    _legacy_row(conn, "f19d7924154a6890", ISSUE_40, first, "2026-09-16T23:07:00+00:00")
    _legacy_row(conn, "6aafd5304c096dfb", ISSUE_49, second, "2026-09-17T00:55:00+00:00")
    _legacy_row(conn, "a7de56f88af0c0a8", gate, other, "2026-09-16T23:30:00+00:00")
    conn.commit()
    conn.close()
    gh.calls.clear()

    reporter = _reporter(gh)
    done = reporter.merge_duplicates()

    assert done == [f"closed {second} as a duplicate of {first}"]
    assert gh.issues[second]["state"] == "CLOSED"
    (comment,) = gh.issues[second]["comments"]
    assert comment.startswith("duplicate of #1")
    assert gh.issues[first]["state"] == "OPEN" and gh.issues[first]["comments"] == []
    assert gh.issues[other]["state"] == "OPEN" and gh.issues[other]["comments"] == []

    rows = {row.issue_url: row for row in deficiencies.ledger(include_all=True)}
    assert set(rows) == {first, other}
    kept = rows[first]
    # The kept row keeps the fingerprint its issue was opened under; the duplicate's and
    # the one today's rule computes are aliases to it, so neither opens a second issue.
    assert kept.fingerprint == "f19d7924154a6890"
    conn = init_db()
    for alias in ("6aafd5304c096dfb", deficiencies.fingerprint(deficiencies.TURN_REPORT, ISSUE_49)):
        assert deficiencies.canonical_fingerprint(conn, alias) == kept.fingerprint
    conn.close()
    assert (kept.count, kept.reported_count, kept.status) == (2, 2, deficiencies.REPORTED)
    assert [e["n"] for e in kept.evidence] == [1, 2]

    # Nothing more at the next start, and the next occurrence is one comment on the kept issue.
    assert reporter.merge_duplicates() == []
    deficiencies.record(deficiencies.TURN_REPORT, ISSUE_49)
    reporter.flush()
    assert len(gh.issues[first]["comments"]) == 1
    assert len(gh.issues) == 3


def test_a_duplicate_whose_issue_cannot_close_is_left_for_the_next_start(ppy_home) -> None:
    gh = FakeGh()
    first = gh(["issue", "create", "--title", "#40", "--label", "turn-report"], "body")[1].strip()
    second = gh(["issue", "create", "--title", "#49", "--label", "turn-report"], "body")[1].strip()
    conn = init_db()
    _legacy_row(conn, "f19d7924154a6890", ISSUE_40, first, "2026-09-16T23:07:00+00:00")
    _legacy_row(conn, "6aafd5304c096dfb", ISSUE_49, second, "2026-09-17T00:55:00+00:00")
    conn.commit()
    conn.close()

    def refuse_close(args: list[str], stdin: str | None = None) -> tuple[int, str, str]:
        if args[:2] == ["issue", "close"]:
            return 1, "", "HTTP 502"
        return gh(args, stdin)

    broken = deficiencies.Reporter(
        forge=deficiencies.GhForge(run=refuse_close),
        config=deficiencies.Settings(repo=RUNTIME_REPO),
    )
    assert broken.merge_duplicates() == []
    assert {row.issue_url for row in deficiencies.ledger(include_all=True)} == {first, second}

    assert _reporter(gh).merge_duplicates() == [f"closed {second} as a duplicate of {first}"]
    (row,) = deficiencies.ledger(include_all=True)
    assert (row.issue_url, row.count) == (first, 2)


def test_a_turn_on_a_shared_agent_is_told_where_facts_go_and_papaya_is_asked_once(
    ppy_home, client_home, ready, registered_repo
) -> None:
    gh, papaya_api = FakeGh(), FakePapaya()
    reads: list[dict[str, str]] = []

    def agent_record(env: dict[str, str]) -> dict[str, Any]:
        reads.append(env)
        return {"id": "agent-1", "ownership_scope": "workspace"}

    def review(turn: Turn) -> str:
        _deliver(turn)
        # A turn that reached for it anyway, and said so.
        return f"Delivered.\nRUNTIME: {ISSUE_49}"

    turns = _review_ticket(review, papaya_api)
    runner = _runner(turns, papaya_api, agent_record=agent_record)

    assert _serve_with(Harness(FakeEvents([EVENT])), client_home, runner, _reporter(gh)) == 0

    brief, reviewed = turns.calls
    assert brief.name == prompts.BRIEF
    for turn in (brief, reviewed):
        assert "- agent: shared" in turn.prompt
        assert "- memory: repo-notes-only" in turn.prompt
        assert prompts.MEMORY_RULE in turn.prompt
    assert ".ppy/memory/repos/<repo>/notes.md" in prompts.MEMORY_RULE
    assert "never to `propose_memory`" in prompts.MEMORY_RULE
    # One read for the connection, however many turns it launches.
    assert len(reads) == 1
    assert papaya.known_agent_kind("agent-1") == papaya.AgentKind(papaya.AGENT_SHARED)

    # The refusal the prompt already accounts for is a prompt defect, not a turn report.
    (row,) = deficiencies.ledger(include_all=True)
    assert row.kind == deficiencies.PROMPT_DEFECT
    assert row.evidence[0]["turn"] == "review"


def _unreadable(_env: dict[str, str]) -> dict[str, Any]:
    raise papaya_events.PapayaEventError("Papaya could not be reached")


@pytest.mark.parametrize(
    ("record", "expected"),
    [(lambda _env: {"ownership_scope": "personal"}, ("personal", "papaya")), (_unreadable, None)],
    ids=["personal", "unreadable"],
)
def test_a_personal_agent_may_propose_memory_and_an_unreadable_record_adds_no_facts(
    ppy_home, client_home, ready, registered_repo, record, expected
) -> None:
    papaya_api = FakePapaya()
    turns = _review_ticket(_deliver, papaya_api)
    runner = _runner(turns, papaya_api, agent_record=record)

    harness = Harness(FakeEvents([EVENT]))
    assert _serve_with(harness, client_home, runner, _reporter(FakeGh())) == 0

    brief = turns.calls[0].prompt
    if expected is None:
        assert "- memory:" not in brief and "- agent:" not in brief
    else:
        assert f"- agent: {expected[0]}" in brief and f"- memory: {expected[1]}" in brief


def test_every_turn_prompt_says_where_durable_facts_go() -> None:
    for turn in prompts.TURNS:
        assert prompts.MEMORY_RULE in prompts.load(turn), turn
    step = prompts.load(prompts.BRIEF).split("6. **Once placed.**", 1)[1].split("\n\n", 1)[0]
    assert "Only when the facts say `memory: papaya`" in step


def test_readiness_notes_a_shared_agent_as_info_and_never_a_blocker(ppy_home, client_home) -> None:
    problems: list[readiness.Problem] = []
    readiness._papaya_problems(problems)
    assert readiness.MEMORY_UNAVAILABLE_SHARED_AGENT not in [p.code for p in problems]

    papaya.remember_agent_kind("agent-1", papaya.AgentKind(papaya.AGENT_SHARED))
    problems = []
    readiness._papaya_problems(problems)
    (note,) = [p for p in problems if p.code == readiness.MEMORY_UNAVAILABLE_SHARED_AGENT]
    assert note.info and not note.blocking and not note.steps
    verdict = readiness.Readiness(state=readiness.READY, problems=problems)
    assert verdict.blockers == [] and verdict.warnings == [] and note in verdict.notes

    papaya.remember_agent_kind("agent-1", papaya.AgentKind(papaya.AGENT_PERSONAL))
    problems = []
    readiness._papaya_problems(problems)
    assert readiness.MEMORY_UNAVAILABLE_SHARED_AGENT not in [p.code for p in problems]


def test_a_readiness_finding_the_runtime_owns_and_cannot_close_is_recorded() -> None:
    theirs = readiness.Problem("no_harness", "no harness", "sign in", owner=readiness.USER)
    ours = readiness.Problem("config_invalid", "cannot read it", "`ppy setup`")
    warning = readiness.Problem("repo_not_onboarded", "x", "y", blocking=False)
    verdict = readiness.Readiness(state=readiness.BLOCKED, problems=[ours, warning])
    assert serve.unremedied_readiness(verdict) == [ours]
    both = readiness.Readiness(state=readiness.BLOCKED, problems=[ours, theirs])
    assert serve.unremedied_readiness(both) == []


def test_ppy_deficiency_list_and_doctor_show_the_ledger(ppy_home, capsys, monkeypatch) -> None:
    deficiencies.record(deficiencies.TURN_REPORT, LINE)
    deficiencies.record(deficiencies.WORKER_DENIAL, "`Bash(terraform:*)`", scope="repo:api")
    _reporter(FakeGh()).flush()

    assert cli.main(["deficiency", "list"]) == 0
    out = capsys.readouterr().out
    assert LINE in out and f"https://github.com/{RUNTIME_REPO}/issues/1" in out
    assert "terraform" not in out

    assert cli.main(["deficiency", "list", "--all"]) == 0
    assert "below threshold" in capsys.readouterr().out

    from papaya_agent_runtime.setup import discovery

    nothing = {"harnesses": [], "requirements": [], "companions": []}
    monkeypatch.setattr(doctor, "discover", lambda: nothing)
    monkeypatch.setattr(discovery, "discover", lambda: nothing)
    text = doctor.render_text(doctor.collect())
    assert "self-report: 1 open self-reported issue(s), 0 waiting to open" in text


def test_serve_says_at_start_when_deficiencies_are_waiting(ppy_home) -> None:
    deficiencies.record(deficiencies.TURN_REPORT, LINE)
    stderr = io.StringIO()
    serve.announce_deficiencies(stderr=stderr)
    assert stderr.getvalue() == (
        "ppy serve: 1 self-reported deficiency is waiting to open as issues — "
        "`ppy deficiency list`\n"
    )


def test_an_exception_is_recorded_with_its_traceback_and_recording_never_raises(
    ppy_home, monkeypatch
) -> None:
    try:
        raise ValueError(f"bad value under {Path.home()}/secret")
    except ValueError as exc:
        deficiencies.record_exception("a manager round", exc)
    (row,) = deficiencies.ledger()
    assert row.detail.startswith("a manager round: ValueError in test_deficiencies.")
    assert (
        "Traceback" in row.evidence[0]["error"] and str(Path.home()) not in row.evidence[0]["error"]
    )

    monkeypatch.setattr(deficiencies, "_record", lambda *a, **k: 1 / 0)
    assert deficiencies.record(deficiencies.TURN_REPORT, "anything") is None


# ── turn reports a later fix answered ──────────────────────────────────────


def _checkin_report(
    ticket: int, run_id: int, worker: int, detail: str, **round_record: Any
) -> None:
    """A round's check-in record, then the check-in turn's `RUNTIME:` line about it."""
    conn = init_db()
    try:
        store.append_event(
            conn,
            kind="ticket_round",
            payload={
                "task_id": ticket,
                "action": "checkin",
                "worker_task_id": worker,
                **round_record,
            },
            run_id=run_id,
            task_id=ticket,
        )
        event_id = int(conn.execute("SELECT MAX(id) FROM events").fetchone()[0])
    finally:
        conn.close()
    deficiencies.record(
        deficiencies.TURN_REPORT,
        detail,
        evidence={
            "turn": "checkin",
            "task_id": ticket,
            "worker_task_id": worker,
            "run_id": run_id,
            "event_id": event_id,
        },
    )


def test_a_turn_report_about_a_push_checkin_the_fix_made_impossible_closes_with_one_comment(
    ppy_home,
) -> None:
    """#46: the report was opened by the pre-fix push trigger, and the fix closes it."""
    conn = init_db()
    run_id = store.create_run(conn, "run 9")
    ticket, worker, other = (store.add_task(conn, run_id=run_id, title=t) for t in "twx")
    conn.close()
    pre_fix = {"trigger": "push", "reason": "nothing pushed in 47 minutes", "remote_sha": "fc0cbdc"}
    _checkin_report(
        ticket, run_id, worker, "the check-in for task 18 said nothing was pushed", **pre_fix
    )
    # Not answered by the fix: a quiet check-in, and a push check-in the fixed trigger made.
    _checkin_report(ticket, run_id, other, "the quiet check-in read no gate", trigger="quiet")
    _checkin_report(
        ticket,
        run_id,
        other,
        "the push check-in after the fix was still wrong",
        **{**pre_fix, "head_sha": "0e0b511", "last_push_at": "2026-09-16T23:55:00+00:00"},
    )
    gh = FakeGh()
    reporter = _reporter(gh)
    reporter.flush()
    urls = {issue["title"].split(": ", 1)[-1]: url for url, issue in gh.issues.items()}
    assert len(urls) == 3

    assert reporter.reclassify() == [
        f"closed {urls['the check-in for task 18 said nothing was pushed']}"
    ]
    assert reporter.reclassify() == []

    closed = gh.issues[urls["the check-in for task 18 said nothing was pushed"]]
    (comment,) = closed["comments"]
    assert closed["state"] == "CLOSED"
    assert comment.startswith("closing: the check-in this turn reported on cannot fire that way")
    assert "git ls-remote" in comment
    for detail in (
        "the quiet check-in read no gate",
        "the push check-in after the fix was still wrong",
    ):
        assert (gh.issues[urls[detail]]["state"], gh.issues[urls[detail]]["comments"]) == (
            "OPEN",
            [],
        )
    statuses = {d.detail: d.status for d in deficiencies.ledger(include_all=True)}
    assert statuses["the check-in for task 18 said nothing was pushed"] == deficiencies.RECLASSIFIED
