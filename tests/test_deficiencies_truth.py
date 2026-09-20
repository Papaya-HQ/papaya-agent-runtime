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


@pytest.mark.parametrize(
    ("seconds_before_start", "opens"),
    [(1, False), (-1, True)],
    ids=["a second before this build started", "a second after it"],
)
def test_the_running_builds_start_of_life_is_the_boundary(
    ppy_home, seconds_before_start: int, opens: bool
) -> None:
    wall = Wall()
    gh = FakeGh()
    reporter = _reporter(gh, wall)  # born now: this build's first moment on this machine
    row = deficiencies.record(
        deficiencies.TURN_REPORT,
        "`ppy gate run` had no gate record",
        clock=_at(wall, seconds=-seconds_before_start),
    )
    assert row is not None

    reporter.flush()

    assert bool(_opened(gh)) is opens
    assert _row(row.fingerprint).status == (deficiencies.REPORTED if opens else deficiencies.STALE)


@pytest.mark.parametrize(
    ("hours_ago", "opens"),
    [(49, False), (47, True)],
    ids=["older than 48 hours", "younger than 48 hours"],
)
def test_old_news_does_not_open_however_long_this_build_has_been_running(
    ppy_home, hours_ago: int, opens: bool
) -> None:
    """The same build all along: age alone is still enough to hold an issue back."""
    _seen_build(BUILD, START - timedelta(days=30))
    wall = Wall()
    gh = FakeGh()
    row = deficiencies.record(
        deficiencies.TURN_REPORT,
        "`ppy review show` diffed the wrong base",
        clock=_at(wall, hours=-hours_ago),
    )
    assert row is not None

    _reporter(gh, wall).flush()

    assert bool(_opened(gh)) is opens
    assert _row(row.fingerprint).status == (deficiencies.REPORTED if opens else deficiencies.STALE)


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
}


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
    assert deficiencies.reduce_turn_report(ONE_CRASH[119]) == "pr|58"
    assert len(set(crash.values())) == 3
    assert crash[103] != crash[104]

    others = {number: _key(detail) for number, detail in OTHER_CAUSES.items()}
    assert len(set(others.values())) == len(OTHER_CAUSES)  # each its own cause
    assert not set(others.values()) & set(crash.values())
    # Named explicitly, because each is a way a looser rule would go wrong.
    assert others[1001] != others[1002], "two AttributeErrors in different functions"
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
