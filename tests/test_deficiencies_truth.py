"""What the self-report channel may say: nothing stale, one issue per cause, and it closes.

Nineteen issues were open on this runtime's repository on 2026-09-20. Nine of them were
one crash fixed three days earlier, four of those nine opened the morning of the 20th
from evidence dated the 17th, one was Papaya correctly keeping another person's work off
this machine, and nothing had ever closed one. Each test here is one of those.

`gh` is faked at the seam `GhForge` has, the clock is injected, and the build the ledger
thinks is running is injected too, so "before this version existed" is a fact a test can
set rather than a wall-clock accident.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from conftest import leaked
from papaya_agent_runtime import deficiencies
from papaya_agent_runtime.state.db import init_db
from test_deficiencies import RUNTIME_REPO, FakeGh

BUILD = "v0.1.22"
START = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)


class Wall:
    """A clock a test moves by hand."""

    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def _reporter(gh: FakeGh, wall: Wall, *, build: str = BUILD, **settings: Any):
    return deficiencies.Reporter(
        forge=deficiencies.GhForge(run=gh),
        clock=wall,
        build=lambda: build,
        config=deficiencies.Settings(repo=RUNTIME_REPO, **settings),
    )


def _at(wall: Wall, **delta: float):
    """A record clock at an offset from ``wall``'s now."""
    moment = wall.now + timedelta(**delta)
    return lambda: moment


def _row(fingerprint: str) -> deficiencies.Deficiency:
    (found,) = [d for d in deficiencies.ledger(include_all=True) if d.fingerprint == fingerprint]
    return found


def _opened(gh: FakeGh) -> list[str]:
    return [
        args[args.index("--title") + 1] for args, _ in gh.calls if args[:2] == ["issue", "create"]
    ]


def _seen_build(build: str, first_seen: datetime) -> None:
    conn = init_db()
    conn.execute(
        "INSERT OR REPLACE INTO runtime_builds (build_id, first_seen) VALUES (?, ?)",
        (build, first_seen.isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()


# ── nothing stale opens ─────────────────────────────────────────────────────


def _flush_after(gh: FakeGh, wall: Wall, detail: str, *, ago: dict, build: str = BUILD) -> bool:
    """Record ``detail`` ``ago`` before now, flush on ``build``, and say whether it opened."""
    row = deficiencies.record(deficiencies.TURN_REPORT, detail, clock=_at(wall, **ago))
    assert row is not None
    _reporter(gh, wall, build=build).flush()
    opened = bool(_opened(gh))
    status = _row(row.fingerprint).status
    assert status == (deficiencies.REPORTED if opened else deficiencies.STALE)
    return opened


@pytest.mark.parametrize(
    ("hours_ago", "opens"),
    [(49, False), (47, True)],
    ids=["older than 48 hours", "younger than 48 hours"],
)
def test_the_staleness_age_is_one_half_of_the_boundary(
    ppy_home, hours_ago: int, opens: bool
) -> None:
    """Both halves must hold: this row was last seen under the version before this one."""
    _seen_build("0.1.20", START - timedelta(days=30))
    wall = Wall()

    assert (
        _flush_after(FakeGh(), wall, "`ppy gate run` had no gate record", ago={"hours": -hours_ago})
        is opens
    )


@pytest.mark.parametrize(
    ("hours_ago", "opens"),
    [(61, False), (59, True)],
    ids=["last seen under the older version", "last seen under this one"],
)
def test_the_running_versions_start_of_life_is_the_other_half(
    ppy_home, hours_ago: int, opens: bool
) -> None:
    """Both rows are old enough; only the one from before this version started is buried."""
    _seen_build("0.1.20", START - timedelta(days=30))
    _seen_build(BUILD, START - timedelta(hours=60))
    wall = Wall()

    assert (
        _flush_after(
            FakeGh(), wall, "`ppy review show` diffed the wrong base", ago={"hours": -hours_ago}
        )
        is opens
    )


def test_an_upgrade_never_buries_what_was_happening_minutes_before_it(ppy_home) -> None:
    """The upgrade says nothing about whether the new version fixed it."""
    _seen_build("0.1.20", START - timedelta(days=30))
    wall = Wall()
    gh = FakeGh()
    row = deficiencies.record(
        deficiencies.TURN_REPORT,
        "the turn could not read the gate record",
        clock=_at(wall, minutes=-5),
    )
    assert row is not None

    # The upgrade: a new version, first seen now.
    _reporter(gh, wall, build="0.1.30").flush()

    assert len(_opened(gh)) == 1
    assert _row(row.fingerprint).status == deficiencies.REPORTED


def test_a_restart_on_the_same_release_buries_nothing(ppy_home) -> None:
    """Restarting, or an uncommitted edit, is not a new version and not a fix."""
    _seen_build(BUILD, START - timedelta(days=30))
    wall = Wall()
    gh = FakeGh()

    # Three days old, and this release has been running all along: still this release's.
    assert _flush_after(gh, wall, "`ppy gate run` had no gate record", ago={"days": -3}) is True


@pytest.mark.parametrize(
    ("described", "released"),
    [
        ("0.1.22", "0.1.22"),
        ("v0.1.22", "0.1.22"),
        ("0.1.22-3-gabc1234", "0.1.22"),
        ("0.1.22-3-gabc1234.dirty", "0.1.22"),
        ("0.1.22.post1+g9f8e7d6", "0.1.22"),
        ("0.0.0+g1234abc", "0.0.0"),
        ("", "unknown"),
    ],
)
def test_the_version_is_the_release_not_the_commit_or_the_dirty_flag(
    described: str, released: str
) -> None:
    """`git describe` moves at every commit; a ledger keyed on that buries everything."""
    assert deficiencies.released_version(described) == released


def test_a_stale_row_that_happens_again_opens_then(ppy_home) -> None:
    """Stale is "not now", not "never": the next occurrence is on this build."""
    wall = Wall()
    gh = FakeGh()
    reporter = _reporter(gh, wall)
    line = "the Papaya tools were not loaded this turn"
    row = deficiencies.record(deficiencies.TURN_REPORT, line, clock=_at(wall, days=-3))
    assert row is not None
    reporter.flush()
    assert _row(row.fingerprint).status == deficiencies.STALE

    wall.advance(minutes=30)
    deficiencies.record(deficiencies.TURN_REPORT, line, clock=wall)
    reporter.flush()

    assert len(_opened(gh)) == 1
    again = _row(row.fingerprint)
    assert (again.status, again.count) == (deficiencies.REPORTED, 2)


def _old_news(n: int) -> str:
    """One of many distinct turn reports. Distinct in *words*: numbers do not vary a cause."""
    letters = "abcdefghijklmnopqrstuvwxyz"
    # Three letters, so no name is a stopword the content-word reduction drops ("at").
    name = letters[n // 26] + letters[n % 26] + "x"
    return f"the turn could not read the {name} fact"


def test_a_backlog_the_upgrade_inherited_opens_nothing_and_nobody_edits_the_ledger(
    ppy_home,
) -> None:
    """154 rows waited in the live ledger on 2026-09-20, at five issues a day."""
    wall = Wall()
    for n in range(150):
        deficiencies.record(deficiencies.TURN_REPORT, _old_news(n), clock=_at(wall, days=-3))
    # One of them is still happening on this build.
    deficiencies.record(deficiencies.TURN_REPORT, _old_news(7), clock=wall)
    gh = FakeGh()

    _reporter(gh, wall).flush()

    assert _opened(gh) == [f"A turn said the runtime got in its way: {_old_news(7)}"]
    statuses = [d.status for d in deficiencies.ledger(include_all=True)]
    assert statuses.count(deficiencies.STALE) == 149


# ── one cause, one issue ────────────────────────────────────────────────────

#: The `RUNTIME:` lines behind the nine issues one crash produced, verbatim from the
#: ledger rows that carry issue urls 103, 104, 105, 106, 107, 117, 118 and 119.
ONE_CRASH = {
    103: (
        "the worker runner crashed with `AttributeError(\"'str' object has no attribute "
        "'get'\")` before task 25 took a turn. This checkout has uncommitted changes on a "
        "branch about denial messages in the Claude provider, which may be the fix, but I "
        "haven't confirmed that."
    ),
    104: (
        "the Claude worker crashes at startup with AttributeError in `claude.live_denial` "
        "(tasks 22, 23, 25), so there was nothing to review or send back."
    ),
    105: (
        "the Claude worker crashes at startup (task 25), and the open fix for it (PR #58) is "
        "failing CI, so this ticket can't move."
    ),
    106: (
        "the Claude worker crashes at startup (task 25), its fix (PR #58) is red on CI, and "
        "the worker limit refuses a Codex worker, so this ticket can't move."
    ),
    107: (
        "the Claude worker crashes at startup (task 25). Its fix (PR #58) is green but not "
        "merged, so this ticket can't move."
    ),
    117: (
        "tasks 25 and 27 crashed on the same AttributeError when reading a live permission "
        "denial, and the fix is in runtime PR #58, which isn't merged."
    ),
    118: (
        "the Claude worker crashes at startup (tasks 25 and 27). The fix (PR #58) passes CI "
        "but isn't merged, so neither ticket can move."
    ),
    119: (
        "tasks 25 and 27 are still blocked on the AttributeError raised when reading a live "
        "permission denial; the fix is in runtime PR #58, which is still open."
    ),
}

#: Reports that are not that crash and must not be folded into it, or into each other.
OTHER_CAUSES = {
    85: (
        "`ppy gate run --task 18` (without `--full`) ran only `make lint-check`, while the "
        "worker's report and the turn facts call `make verify` this repository's registered "
        "local gate."
    ),
    120: (
        "the Papaya tools were loaded on demand this turn, so the work item could be read. "
        "The dispatch printed no warnings."
    ),
    47: (
        "gate run for task 19 shared the local test database with other in-flight tasks, and "
        "it restarted mid-run, so the full suite could not produce a clean result."
    ),
    40: (
        "propose_memory rejected an agent-scoped proposal for agent dfe335d3 (machine-"
        "extracted memory is only allowed on a personal agent), and its provenance field "
        "accepts no work-item source type."
    ),
    # A second AttributeError, somewhere else entirely: the class alone is not a cause.
    1001: "the rounds crashed with AttributeError in `rounds.observe_pr`, losing the check-in.",
    1002: "a turn crashed with AttributeError in `serve.waiting_reason` after every check-in.",
    # Two reports about different pull requests are two reports.
    1003: "the fix for this is in PR #94, which is still open, so the ticket can't move.",
    # Two messages of one class with nowhere named: the message is the cause, whole.
    1004: "the turn ended on AttributeError: 'NoneType' object has no attribute 'get'",
    1005: (
        "the turn ended on AttributeError: 'NoneType' object has no attribute 'phase' when "
        "parking the ticket"
    ),
    1006: "the check-in raised AttributeError: 'Settings' object has no attribute 'max_per_day'",
    1007: "the check-in raised AttributeError: 'Settings' object has no attribute 'repo'",
    # Numbers that are counted, not named: none of these is a report.
    1008: "issue 3 of 5 checks failed on the delivered branch and the lane gave up",
    1009: "the gate ran 12 issues 4 times before the worker was sent back",
    1010: "PAP-219 #3 came back with nothing to show after the brief turn",
}

#: The same repository's PR #58 and another repository's: one number, two things.
OTHER_REPO_PR = "the fix is in PR #58 in repo `papaya-backend-monorepo`, which is still open."


def _key(detail: str) -> str:
    return deficiencies.fingerprint(deficiencies.TURN_REPORT, detail)


def test_the_nine_issues_one_crash_opened_would_be_three_and_none_of_them_is_another_cause() -> (
    None
):
    """Six collapse on the pull request they name; two stay apart, which is the safe side.

    #103 quotes only the exception's message and #104 names only `claude.live_denial`;
    neither names PR #58, and nothing in either line ties it to the other six without a
    rule loose enough to merge unrelated reports. Three issues instead of nine.
    """
    crash = {number: _key(detail) for number, detail in ONE_CRASH.items()}
    assert crash[105] == crash[106] == crash[107] == crash[117] == crash[118] == crash[119]
    assert deficiencies.reduce_turn_report(ONE_CRASH[119]) == "pr|58|"
    assert len(set(crash.values())) == 3
    assert crash[103] != crash[104]

    others = {number: _key(detail) for number, detail in OTHER_CAUSES.items()}
    assert len(set(others.values())) == len(OTHER_CAUSES)  # each its own cause
    assert not set(others.values()) & set(crash.values())
    # Named explicitly, because each is a way a looser rule would go wrong.
    assert others[1001] != others[1002], "two AttributeErrors in different functions"
    assert others[1004] != others[1005], "two messages of one class, differing at the end"
    assert others[1006] != others[1007], "two 'Settings' object messages"
    for enumeration in (1008, 1009, 1010):
        reduced = deficiencies.reduce_turn_report(OTHER_CAUSES[enumeration])
        assert not reduced.startswith("pr|"), f"{enumeration} names no report: {reduced}"
    # One pull request number in two repositories is two causes.
    assert _key(OTHER_REPO_PR) != crash[105]
    assert deficiencies.reduce_turn_report(OTHER_REPO_PR) == "pr|58|papaya-backend-monorepo"
    # A passing mention never takes over from the exception and the place it came from.
    passing = "the worker died on AttributeError in `claude.live_denial`; the fix is PR #58."
    assert _key(passing) == crash[104] != crash[105]
    assert others[1003] != crash[105], "two different pull requests"
    assert others[1001] != crash[117], "a report naming PR #58 and one that merely crashed"


def test_the_same_cause_worded_five_ways_is_one_issue_with_four_comments(ppy_home) -> None:
    wall = Wall()
    gh = FakeGh()
    reporter = _reporter(gh, wall)

    for number in (105, 106, 107, 118, 119):
        deficiencies.record(deficiencies.TURN_REPORT, ONE_CRASH[number], clock=wall)
        reporter.flush()

    assert len(_opened(gh)) == 1
    (issue,) = gh.created()
    assert len(issue["comments"]) == 4


# ── a rule change does not open a second issue for what already has one ─────


def test_a_wording_whose_fingerprint_moved_still_lands_on_its_own_issue(ppy_home) -> None:
    """The alias table: the row keeps its issue's fingerprint, today's key points at it."""
    wall = Wall()
    gh = FakeGh()
    detail = ONE_CRASH[119]
    legacy = "0123456789abcdef"  # what an older rule keyed this line by
    deficiencies.record(deficiencies.TURN_REPORT, detail, clock=wall)
    conn = init_db()
    conn.execute(
        "UPDATE deficiencies SET fingerprint = ?, issue_url = ?, status = ?, reported_count = 1 "
        "WHERE fingerprint = ?",
        (
            legacy,
            f"https://github.com/{RUNTIME_REPO}/issues/119",
            deficiencies.REPORTED,
            _key(detail),
        ),
    )
    conn.commit()
    conn.close()
    gh.issues[f"https://github.com/{RUNTIME_REPO}/issues/119"] = {
        "title": "#119",
        "body": "",
        "labels": [],
        "state": "OPEN",
        "comments": [],
    }
    reporter = _reporter(gh, wall)

    reporter.merge_duplicates()
    wall.advance(minutes=5)
    deficiencies.record(deficiencies.TURN_REPORT, ONE_CRASH[118], clock=wall)  # same cause
    reporter.flush()

    assert _opened(gh) == []
    assert len(gh.issues[f"https://github.com/{RUNTIME_REPO}/issues/119"]["comments"]) == 1
    (row,) = deficiencies.ledger(include_all=True)
    assert (row.fingerprint, row.count) == (legacy, 2)


# ── an issue that is over closes itself ─────────────────────────────────────


def _reported(wall: Wall, gh: FakeGh, detail: str = "`ppy gate run` had no gate record"):
    """One deficiency with an open issue, recorded and opened now."""
    reporter = _reporter(gh, wall)
    row = deficiencies.record(deficiencies.TURN_REPORT, detail, clock=wall)
    assert row is not None
    assert reporter.flush()
    return reporter, row.fingerprint


def test_an_issue_nobody_has_seen_for_a_week_closes_itself_once_a_newer_build_has_run(
    ppy_home,
) -> None:
    wall = Wall()
    gh = FakeGh()
    reporter, key = _reported(wall, gh)
    url = _row(key).issue_url

    # A week later, on the same build: still open. Nothing says it is fixed.
    wall.advance(days=8)
    assert reporter.flush() == []
    assert gh.issues[url]["state"] == "OPEN"

    # The runtime is upgraded, and the next flush is the first this build has made.
    newer = _reporter(gh, wall, build="v0.1.30")
    done = newer.flush()

    assert done == [f"closed {url}: not seen since {_row(key).last_seen}"]
    assert gh.issues[url]["state"] == "CLOSED"
    (comment,) = gh.issues[url]["comments"]
    assert comment.startswith("closing: not seen since")
    assert "7 days" in comment
    assert _row(key).closed_at is not None

    # Said once: another flush on the same build does not close it again.
    assert newer.flush() == []
    assert len(gh.issues[url]["comments"]) == 1


def test_a_forge_that_comments_and_then_refuses_the_close_says_nothing_twice(ppy_home) -> None:
    """`gh issue close --comment` is two things, and the first can land without the second."""
    wall = Wall()
    gh = FakeGh()
    refused: list[bool] = [True]

    def half_open(args: list[str], stdin: str | None = None) -> tuple[int, str, str]:
        if args[:2] == ["issue", "close"] and refused[0]:
            if "--comment" in args:  # the comment lands, the close does not
                gh.issues[args[2]]["comments"].append(args[args.index("--comment") + 1])
            return 1, "", "HTTP 502"
        return gh(args, stdin)

    _reported(wall, gh)
    (row,) = deficiencies.ledger(include_all=True)
    url = row.issue_url
    wall.advance(days=8)
    broken = deficiencies.Reporter(
        forge=deficiencies.GhForge(run=half_open),
        clock=wall,
        build=lambda: "0.1.30",
        config=deficiencies.Settings(repo=RUNTIME_REPO),
    )

    assert broken.flush() == []  # the close failed
    assert gh.issues[url]["state"] == "OPEN"
    assert len(gh.issues[url]["comments"]) == 1

    # Inside the backoff nothing is tried again at all, however often it flushes.
    for _ in range(3):
        assert broken.flush() == []
    assert len(gh.issues[url]["comments"]) == 1

    # After the backoff it is tried again, and the comment it already said is not said twice.
    wall.advance(seconds=deficiencies.CLOSE_RETRY_AFTER_SECONDS + 1)
    refused[0] = False
    assert len(broken.flush()) == 1
    assert gh.issues[url]["state"] == "CLOSED"
    assert len(gh.issues[url]["comments"]) == 1


def test_a_refused_close_costs_one_of_the_days_closes(ppy_home) -> None:
    """A forge that refuses must cost no more than a forge that agrees."""
    wall = Wall()
    gh = FakeGh()
    tries: list[list[str]] = []

    def refuse(args: list[str], stdin: str | None = None) -> tuple[int, str, str]:
        if args[:2] == ["issue", "close"]:
            tries.append(list(args))
            return 1, "", "HTTP 502"
        return gh(args, stdin)

    reporter = _reporter(gh, wall, max_per_day=2)
    for n in range(4):
        deficiencies.record(deficiencies.TURN_REPORT, _old_news(n), clock=wall)
    reporter.flush()
    wall.advance(days=1)
    reporter.flush()
    assert len(_opened(gh)) == 4

    wall.advance(days=9)
    broken = deficiencies.Reporter(
        forge=deficiencies.GhForge(run=refuse),
        clock=wall,
        build=lambda: "0.1.30",
        config=deficiencies.Settings(repo=RUNTIME_REPO, max_per_day=2),
    )
    assert broken.flush() == []

    assert len(tries) == 2  # not four: a failed attempt is still one of the day's two
    assert [row.closed_at for row in deficiencies.ledger(include_all=True)] == [None] * 4


def test_a_recurrence_reopens_what_closed_itself(ppy_home) -> None:
    wall = Wall()
    gh = FakeGh()
    _reported(wall, gh)
    wall.advance(days=8)
    newer = _reporter(gh, wall, build="v0.1.30")
    newer.flush()
    (row,) = deficiencies.ledger(include_all=True)
    assert gh.issues[row.issue_url]["state"] == "CLOSED"

    deficiencies.record(deficiencies.TURN_REPORT, row.detail, clock=wall)
    newer.flush()

    assert gh.issues[row.issue_url]["state"] == "OPEN"
    again = _row(row.fingerprint)
    assert (again.status, again.closed_at) == (deficiencies.REPORTED, None)


def test_a_quiet_issue_stays_open_while_it_is_still_being_recorded(ppy_home) -> None:
    """Quiet for a week is not enough on its own: it must have been quiet *across* a build."""
    wall = Wall()
    gh = FakeGh()
    reporter, key = _reported(wall, gh)
    url = _row(key).issue_url
    wall.advance(days=3)
    deficiencies.record(deficiencies.TURN_REPORT, _row(key).detail, clock=wall)
    wall.advance(days=5)  # eight days after it opened, but only five since it was seen

    assert _reporter(gh, wall, build="v0.1.30").flush() != []  # the recurrence is commented
    assert gh.issues[url]["state"] == "OPEN"


# ── a retired kind points at its successor ──────────────────────────────────


def test_a_retired_kinds_issue_is_closed_once_pointing_at_its_successor(ppy_home) -> None:
    """#86 (`idle-work-refused`, 28 comments) was replaced in PR 111 and left open."""
    wall = Wall()
    gh = FakeGh()
    reporter = _reporter(gh, wall)
    deficiencies.record(
        deficiencies.REPEATED_WITHOUT_PROGRESS, "a ticket kept coming back", clock=wall
    )
    deficiencies.record(
        deficiencies.REPEATED_WITHOUT_PROGRESS, "a ticket kept coming back", clock=wall
    )
    old = deficiencies.record(deficiencies.IDLE_WORK_REFUSED, "not_routed_here refusal", clock=wall)
    assert old is not None
    successor, retired = f"https://github.com/{RUNTIME_REPO}/issues/1", None
    gh.issues[successor] = {"title": "", "body": "", "labels": [], "state": "OPEN", "comments": []}
    conn = init_db()
    conn.execute(
        "UPDATE deficiencies SET status = ?, issue_url = ?, opened_at = ?, reported_count = count "
        "WHERE kind = ?",
        (
            deficiencies.REPORTED,
            successor,
            wall.now.isoformat(),
            deficiencies.REPEATED_WITHOUT_PROGRESS,
        ),
    )
    retired = f"https://github.com/{RUNTIME_REPO}/issues/86"
    gh.issues[retired] = {"title": "", "body": "", "labels": [], "state": "OPEN", "comments": []}
    conn.execute(
        "UPDATE deficiencies SET status = ?, issue_url = ?, opened_at = ?, reported_count = count "
        "WHERE kind = ?",
        (deficiencies.REPORTED, retired, wall.now.isoformat(), deficiencies.IDLE_WORK_REFUSED),
    )
    conn.commit()
    conn.close()

    done = reporter.flush()

    assert done == [f"closed {retired} as superseded"]
    assert gh.issues[retired]["state"] == "CLOSED"
    (comment,) = gh.issues[retired]["comments"]
    assert comment.startswith(f"superseded by `{deficiencies.REPEATED_WITHOUT_PROGRESS}`")
    assert successor in comment
    assert _row(old.fingerprint).status == deficiencies.SUPERSEDED
    # Once, and the successor's own issue is untouched.
    assert reporter.flush() == []
    assert gh.issues[successor]["comments"] == []


def test_a_retired_kind_never_opens_an_issue_in_the_first_place(ppy_home) -> None:
    wall = Wall()
    gh = FakeGh()
    row = deficiencies.record(deficiencies.IDLE_WORK_REFUSED, "not_routed_here refusal", clock=wall)
    assert row is not None and row.status == deficiencies.PENDING

    _reporter(gh, wall).flush()

    assert _opened(gh) == []
    assert _row(row.fingerprint).status == deficiencies.SUPERSEDED


# ── a database written by another build ─────────────────────────────────────


def test_a_schema_24_ledger_migrates_with_its_rows_and_keeps_reporting(ppy_home) -> None:
    """An upgrade meets a database with rows in it, and must add columns, not lose them."""
    from papaya_agent_runtime.paths import db_path
    from papaya_agent_runtime.state.db import SCHEMA_VERSION, schema_version

    conn = init_db()
    # The `deficiencies` table as schema 24 wrote it, with the rows a live ledger has.
    conn.execute("DROP TABLE deficiencies")
    conn.execute(
        "CREATE TABLE deficiencies (fingerprint TEXT PRIMARY KEY, kind TEXT NOT NULL, "
        "title TEXT NOT NULL, detail TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, "
        "count INTEGER NOT NULL DEFAULT 1, evidence TEXT NOT NULL DEFAULT '[]', issue_url TEXT, "
        "status TEXT NOT NULL DEFAULT 'watching', opened_at TEXT, "
        "reported_count INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO deficiencies (fingerprint, kind, title, detail, first_seen, last_seen, "
        "count, evidence, issue_url, status, opened_at, reported_count) "
        "VALUES ('old1', ?, 'A turn said', 'the turn could not read the gate record', ?, ?, "
        "2, '[]', ?, ?, ?, 2)",
        (
            deficiencies.TURN_REPORT,
            START.isoformat(),
            START.isoformat(),
            f"https://github.com/{RUNTIME_REPO}/issues/9",
            deficiencies.REPORTED,
            START.isoformat(),
        ),
    )
    conn.execute("DROP TABLE deficiency_aliases")
    conn.execute("DROP TABLE runtime_builds")
    conn.execute("PRAGMA user_version = 24")
    conn.commit()
    conn.close()

    migrated = init_db(db_path())
    assert schema_version(migrated) == SCHEMA_VERSION
    migrated.close()

    (row,) = deficiencies.ledger(include_all=True)
    assert (row.count, row.status, row.closed_at) == (2, deficiencies.REPORTED, None)
    # And it goes on working: the row is quiet, and an upgrade closes it.
    wall = Wall(START + timedelta(days=8))
    gh = FakeGh()
    gh.issues[str(row.issue_url)] = {
        "title": "",
        "body": "",
        "labels": [],
        "state": "OPEN",
        "comments": [],
    }

    assert _reporter(gh, wall, build="0.1.30").flush() == [
        f"closed {row.issue_url}: not seen since {row.last_seen}"
    ]


# ── the bounds and the privacy are the same as they were ────────────────────


def test_closing_is_bounded_by_max_per_day_like_opening(ppy_home) -> None:
    wall = Wall()
    gh = FakeGh()
    reporter = _reporter(gh, wall, max_per_day=2)
    for n in range(4):
        deficiencies.record(deficiencies.TURN_REPORT, _old_news(n), clock=wall)
    reporter.flush()
    assert len(_opened(gh)) == 2  # two a day, so four take two days
    wall.advance(days=1)
    reporter.flush()
    assert len(_opened(gh)) == 4

    wall.advance(days=9)
    newer = _reporter(gh, wall, build="v0.1.30", max_per_day=2)
    assert len(newer.flush()) == 2  # closes are capped the same way
    wall.advance(days=1)
    assert len(newer.flush()) == 2
    assert sum(1 for issue in gh.issues.values() if issue["state"] == "CLOSED") == 4


def test_self_reporting_turned_off_opens_comments_and_closes_nothing(ppy_home) -> None:
    wall = Wall()
    gh = FakeGh()
    deficiencies.record(deficiencies.TURN_REPORT, "`ppy gate run` had no record", clock=wall)
    deficiencies.record(deficiencies.IDLE_WORK_REFUSED, "not_routed_here refusal", clock=wall)
    off = _reporter(gh, wall, enabled=False)

    assert off.flush() == []
    wall.advance(days=9)
    assert off.flush() == []

    assert gh.calls == []
    assert {d.status for d in deficiencies.ledger(include_all=True)} == {deficiencies.PENDING}


def test_nothing_private_is_in_a_closing_comment(ppy_home, privacy_leaks) -> None:
    wall = Wall()
    gh = FakeGh()
    detail = f"`ppy gate run` failed in {privacy_leaks['home']} ({privacy_leaks['email']})"
    _reported(wall, gh, detail)
    wall.advance(days=8)

    _reporter(gh, wall, build="v0.1.30").flush()

    for args, stdin in gh.calls:
        assert not leaked(" ".join(args) + (stdin or ""))


# ── a count of two is two in one place ──────────────────────────────────────
#
# Issue #84 said "A check-in steered twice for the same reason: midpoint" and its
# evidence was five different tickets, each steered once at its midpoint; the midpoint
# check-in fires at most once per worker, so no worker had been steered twice at all.
# The threshold is documented as occurrences "within one scope", and it was counted
# that way — but the scope went through `redact` before it was stored, and a Papaya
# work item id is a UUID, which redaction reads as an opaque key. Every ticket reached
# the ledger as the one scope `ticket:[redacted]`, so two tickets were one.

#: Every kind that claims something happened more than once in one place. The tests
#: below iterate this, so a kind given a threshold later is held to the same rule
#: without anybody editing them.
THRESHOLD_KINDS = sorted(k for k, spec in deficiencies.KINDS.items() if spec.threshold > 1)


def _occurrence(kind: str, scope: str, task_id: int, wall: Wall) -> None:
    """One occurrence of ``kind`` in ``scope``, distinguishable from every other."""
    deficiencies.record(
        kind,
        "a plain detail the normaliser leaves alone",
        scope=scope,
        evidence={"task_id": task_id, "ticket": f"PAP-{task_id}"},
        clock=wall,
    )


@pytest.mark.parametrize("kind", THRESHOLD_KINDS)
def test_occurrences_in_different_scopes_never_reach_a_threshold(kind, ppy_home) -> None:
    wall = Wall()
    gh = FakeGh()
    spec = deficiencies.KINDS[kind]

    # One more occurrence than the threshold, each of them in a scope of its own.
    for n in range(spec.threshold + 1):
        _occurrence(kind, f"repo:r{n}", n, wall)

    assert _reporter(gh, wall).flush() == []
    assert gh.created() == []
    assert {d.status for d in deficiencies.ledger(include_all=True)} == {deficiencies.WATCHING}


@pytest.mark.parametrize("kind", THRESHOLD_KINDS)
def test_a_threshold_reached_in_one_scope_opens_one_issue_about_that_scope(kind, ppy_home) -> None:
    wall = Wall()
    gh = FakeGh()
    spec = deficiencies.KINDS[kind]
    if spec.superseded_by:  # pragma: no cover - no retired kind has a threshold today
        pytest.skip(f"{kind} is retired, and a retired kind opens nothing")

    _occurrence(kind, "repo:before", 99, wall)
    for n in range(1, spec.threshold + 1):
        _occurrence(kind, "repo:here", n, wall)
    _occurrence(kind, "repo:after", 98, wall)

    assert len(_reporter(gh, wall).flush()) == 1
    (issue,) = gh.created()
    assert issue["title"].startswith(spec.title)
    # The evidence of an issue about one scope is that scope's occurrences and no
    # others: another ticket's is not evidence that this one repeated.
    for n in range(1, spec.threshold + 1):
        assert f"task `{n}`" in issue["body"]
    assert "task `99`" not in issue["body"] and "task `98`" not in issue["body"]
    assert f"seen {spec.threshold} time(s)" in issue["body"]


def test_a_ticket_scope_redaction_blanks_still_names_one_ticket(ppy_home) -> None:
    """The defect behind #84: two work item ids, two scopes, and neither one stored."""
    wall = Wall()
    gh = FakeGh()
    one = "e80cc802-8294-4f7a-837c-69150a385171"
    two = "d0b537fc-99a3-477f-8433-05a069144ee1"

    for item in (one, two):
        _occurrence(deficiencies.REPEATED_STEER, f"ticket:{item}", 1, wall)

    (row,) = deficiencies.ledger(include_all=True)
    assert row.status == deficiencies.WATCHING
    assert len({e["scope"] for e in row.evidence}) == 2
    assert one not in str(row.evidence) and two not in str(row.evidence)
    assert _reporter(gh, wall).flush() == []

    # The same ticket a second time is what the title claims, and it opens.
    _occurrence(deficiencies.REPEATED_STEER, f"ticket:{one}", 2, wall)
    assert _row(row.fingerprint).status == deficiencies.PENDING
    assert len(_reporter(gh, wall).flush()) == 1


def _miscounted(wall: Wall, gh: FakeGh, tickets: int = 5) -> deficiencies.Deficiency:
    """Issue #84 as the live ledger holds it: N tickets, one blanked scope, one issue."""
    url = f"https://github.com/{RUNTIME_REPO}/issues/84"
    at = wall.now.isoformat(timespec="seconds")
    evidence = [
        {
            "at": at,
            "trigger": "midpoint",
            "ticket": deficiencies.REDACTED,
            "task_id": 17 + n,
            "scope": f"ticket:{deficiencies.REDACTED}",
            "n": n + 1,
        }
        for n in range(tickets)
    ]
    key = deficiencies.fingerprint(deficiencies.REPEATED_STEER, "midpoint")
    conn = init_db()
    conn.execute(
        "INSERT INTO deficiencies (fingerprint, kind, title, detail, first_seen, last_seen, "
        "count, evidence, issue_url, status, opened_at, reported_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            key,
            deficiencies.REPEATED_STEER,
            "A check-in steered twice for the same reason: midpoint",
            "midpoint",
            at,
            at,
            tickets,
            json.dumps(evidence),
            url,
            deficiencies.REPORTED,
            at,
            tickets,
        ),
    )
    conn.commit()
    conn.close()
    gh.issues[url] = {"title": "steered twice", "body": "", "labels": [], "state": "OPEN"}
    gh.issues[url]["comments"] = []
    return _row(key)


def test_an_issue_opened_on_a_miscount_corrects_itself_and_closes_once(ppy_home) -> None:
    wall = Wall()
    gh = FakeGh()
    row = _miscounted(wall, gh)
    url = str(row.issue_url)
    reporter = _reporter(gh, wall)

    done = reporter.flush()

    assert done == [f"closed {url}: it never happened in one place"]
    assert gh.issues[url]["state"] == "CLOSED"
    (comment,) = gh.issues[url]["comments"]
    assert comment.startswith("closing: this never happened 2 times in one place.")
    assert "5 occurrence(s) across 5 scope(s)" in comment

    # The row keeps its fingerprint — the rule changed how occurrences are counted, not
    # what a deficiency is called, so no alias is needed and none is written.
    corrected = _row(row.fingerprint)
    assert corrected.status == deficiencies.WATCHING
    assert corrected.issue_url is None
    assert corrected.count == 5  # the ledger is re-read, never re-written

    # Said once: neither this flush nor a later one says it again or opens anything.
    assert reporter.flush() == []
    wall.advance(days=1)
    assert reporter.flush() == []
    assert len(gh.issues[url]["comments"]) == 1
    assert _opened(gh) == []


def test_a_corrected_row_still_reports_when_it_does_happen_twice_in_one_place(ppy_home) -> None:
    wall = Wall()
    gh = FakeGh()
    row = _miscounted(wall, gh)
    reporter = _reporter(gh, wall)
    assert reporter.flush()  # the miscounted issue closes

    for _ in range(2):
        deficiencies.record(
            deficiencies.REPEATED_STEER,
            "midpoint",
            scope="ticket:PAP-231",
            evidence={"ticket": "PAP-231", "task_id": 300},
            clock=wall,
        )

    assert len(reporter.flush()) == 1
    assert _row(row.fingerprint).status == deficiencies.REPORTED
    body = gh.created()[-1]["body"]
    assert "ticket `PAP-231`" in body
    assert "seen 2 time(s)" in body
    assert deficiencies.REDACTED not in body  # not one of the five blanked occurrences


def test_a_pending_row_left_by_an_older_build_opens_nothing(ppy_home) -> None:
    """Between the miscounting build and this one, a row can be `pending` already."""
    wall = Wall()
    gh = FakeGh()
    row = _miscounted(wall, gh)
    conn = init_db()
    conn.execute(
        "UPDATE deficiencies SET status = ?, issue_url = NULL, reported_count = 0 "
        "WHERE fingerprint = ?",
        (deficiencies.PENDING, row.fingerprint),
    )
    conn.commit()
    conn.close()

    assert _reporter(gh, wall).flush() == []
    assert _opened(gh) == []
    assert _row(row.fingerprint).status == deficiencies.WATCHING


def test_a_forge_that_refuses_the_correcting_close_is_tried_again_later(ppy_home) -> None:
    wall = Wall()
    gh = FakeGh()
    refused = [True]

    def refuse(args: list[str], stdin: str | None = None) -> tuple[int, str, str]:
        if args[:2] == ["issue", "close"] and refused[0]:
            return 1, "", "HTTP 502"
        return gh(args, stdin)

    row = _miscounted(wall, gh)
    url = str(row.issue_url)
    broken = deficiencies.Reporter(
        forge=deficiencies.GhForge(run=refuse),
        clock=wall,
        build=lambda: BUILD,
        config=deficiencies.Settings(repo=RUNTIME_REPO),
    )

    assert broken.flush() == []
    assert _row(row.fingerprint).status == deficiencies.REPORTED  # still reported, still true
    for _ in range(3):
        assert broken.flush() == []  # inside the back-off the forge is left alone

    wall.advance(seconds=deficiencies.CLOSE_RETRY_AFTER_SECONDS + 1)
    refused[0] = False
    assert broken.flush() == [f"closed {url}: it never happened in one place"]
    assert _row(row.fingerprint).status == deficiencies.WATCHING
