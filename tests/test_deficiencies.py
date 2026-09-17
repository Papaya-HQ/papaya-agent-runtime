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

import test_serve
from papaya_agent_runtime import cli, deficiencies, papaya_events, prompts, serve
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


def test_a_readiness_finding_the_runtime_owns_and_cannot_close_is_recorded() -> None:
    from papaya_agent_runtime import readiness

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
