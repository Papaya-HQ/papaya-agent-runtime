"""A runtime that cannot work has to say so, once, to someone who can act.

The failure this guards is silent, which is what makes it expensive. From Papaya's
side a connected agent whose runtime was never configured looks perfectly healthy:
the connection is live, the listener is running, events are consumed. The first job
starts a harness in a home with no config, produces nothing, and writes a zero-byte
log nobody reads. Observed on a real machine on 2026-09-15.
"""

from __future__ import annotations

import pytest

from papaya_agent_runtime import readiness
from papaya_agent_runtime.state import init_db, store


@pytest.fixture
def quiet_machine(monkeypatch, ppy_home):
    """A machine with everything present, so each test breaks exactly one thing."""
    monkeypatch.setattr(readiness, "_harness_problems", lambda problems: None)
    monkeypatch.setattr(readiness, "_papaya_problems", lambda problems: None)
    monkeypatch.setattr(readiness, "_config_problems", lambda problems: None)
    monkeypatch.setattr(readiness, "_repo_problems", lambda problems: None)
    return ppy_home


def test_a_machine_with_nothing_wrong_is_ready(quiet_machine) -> None:
    verdict = readiness.check()
    assert verdict.state == readiness.READY
    assert verdict.problems == []


def test_an_unconfigured_runtime_is_blocked_and_says_it_is_its_own_job(
    ppy_home, monkeypatch
) -> None:
    """The exact case that shipped silently: connected, listening, cannot work."""
    monkeypatch.setattr(readiness, "_harness_problems", lambda problems: None)
    monkeypatch.setattr(readiness, "_papaya_problems", lambda problems: None)
    verdict = readiness.check()
    assert verdict.state == readiness.BLOCKED
    no_config = [p for p in verdict.problems if p.code == "no_config"]
    assert no_config, [p.code for p in verdict.problems]
    assert no_config[0].owner == readiness.RUNTIME


def test_no_registered_repositories_blocks_and_needs_the_user(ppy_home, monkeypatch) -> None:
    """Registering is the user's call, so the runtime must not pretend it can."""
    monkeypatch.setattr(readiness, "_harness_problems", lambda problems: None)
    monkeypatch.setattr(readiness, "_papaya_problems", lambda problems: None)
    monkeypatch.setattr(readiness, "_config_problems", lambda problems: None)
    verdict = readiness.check()
    assert [p.code for p in verdict.blockers] == ["no_repos"]
    assert verdict.blockers[0].owner == readiness.USER


def test_a_missing_papaya_connection_is_never_blocking(ppy_home, monkeypatch) -> None:
    """A runtime that refuses to build code because a workspace is unreachable is worse."""
    monkeypatch.setattr(readiness, "_harness_problems", lambda problems: None)
    monkeypatch.setattr(readiness, "_config_problems", lambda problems: None)
    monkeypatch.setattr(readiness, "_repo_problems", lambda problems: None)
    verdict = readiness.check()
    assert [p.code for p in verdict.problems] == ["papaya_not_connected"]
    assert verdict.state == readiness.DEGRADED
    assert verdict.blockers == []


def test_a_repository_that_was_never_onboarded_is_a_gap_not_a_block(ppy_home, monkeypatch) -> None:
    monkeypatch.setattr(readiness, "_harness_problems", lambda problems: None)
    monkeypatch.setattr(readiness, "_papaya_problems", lambda problems: None)
    monkeypatch.setattr(readiness, "_config_problems", lambda problems: None)
    conn = init_db()
    store.add_repo(
        conn,
        name="app",
        origin="https://github.com/acme/app.git",
        local_path="/l",
        default_branch="main",
        base_sha="a" * 40,
        forge_url="https://github.com/acme/app",
    )
    verdict = readiness.check()
    assert verdict.state == readiness.DEGRADED
    assert [p.code for p in verdict.problems] == ["repo_not_onboarded"]


def test_a_repository_with_no_forge_blocks_delivery_and_needs_the_user(
    ppy_home, monkeypatch
) -> None:
    monkeypatch.setattr(readiness, "_harness_problems", lambda problems: None)
    monkeypatch.setattr(readiness, "_papaya_problems", lambda problems: None)
    monkeypatch.setattr(readiness, "_config_problems", lambda problems: None)
    conn = init_db()
    store.add_repo(
        conn,
        name="app",
        origin="/local/only",
        local_path="/l",
        default_branch="main",
        base_sha="a" * 40,
    )
    verdict = readiness.check()
    codes = [p.code for p in verdict.blockers]
    assert "repo_without_forge" in codes
    forge = next(p for p in verdict.problems if p.code == "repo_without_forge")
    assert forge.owner == readiness.USER


# ── Saying it once ──────────────────────────────────────────────────────────


def _verdict(*codes: str) -> readiness.Readiness:
    problems = [readiness.Problem(code=c, summary=c, fix="x") for c in codes]
    return readiness.Readiness(state=readiness.BLOCKED, problems=problems)


def test_the_same_problems_fingerprint_the_same_whatever_their_order() -> None:
    assert _verdict("a", "b").fingerprint == _verdict("b", "a").fingerprint


def test_a_different_set_of_problems_is_new_news() -> None:
    assert _verdict("a").fingerprint != _verdict("a", "b").fingerprint


def test_an_unchanged_verdict_is_reported_once(ppy_home) -> None:
    """Otherwise every wake DMs the owner the same thing forever."""
    conn = init_db()
    verdict = _verdict("no_config")
    assert not readiness.already_reported(conn, verdict)
    readiness.mark_reported(conn, verdict)
    assert readiness.already_reported(conn, verdict)


def test_a_new_problem_speaks_however_soon_it_appears(ppy_home) -> None:
    conn = init_db()
    readiness.mark_reported(conn, _verdict("no_config"))
    assert not readiness.already_reported(conn, _verdict("no_config", "no_harness"))


def test_forgetting_makes_the_next_check_speak_again(ppy_home) -> None:
    conn = init_db()
    verdict = _verdict("no_config")
    readiness.mark_reported(conn, verdict)
    readiness.forget_reports(conn)
    assert not readiness.already_reported(conn, verdict)


# ── What the owner actually reads ───────────────────────────────────────────


def test_the_report_separates_what_needs_them_from_what_the_agent_will_do() -> None:
    """A message that blames the reader for the agent's own chores is a complaint."""
    verdict = readiness.Readiness(
        state=readiness.BLOCKED,
        problems=[
            readiness.Problem("no_config", "never set up", "`ppy setup`", readiness.RUNTIME),
            readiness.Problem("no_harness", "not signed in", "sign in", readiness.USER),
        ],
    )
    text = readiness.report(verdict, agent="@engineering_agent", where="reptar:~/code")
    assert "@engineering_agent" in text
    assert "reptar:~/code" in text  # they are usually not at the machine
    assert text.index("Needs you") < text.index("not signed in")
    assert text.index("handle these myself") < text.index("never set up")


def test_a_ready_runtime_reports_plainly_rather_than_listing_nothing() -> None:
    text = readiness.report(readiness.Readiness(state=readiness.READY), agent="@a")
    assert "ready to take work" in text
    assert "Needs you" not in text


def test_a_blocked_runtime_with_nothing_for_them_says_so() -> None:
    """Otherwise the owner reads a blocked message and has no idea what to do."""
    verdict = readiness.Readiness(
        state=readiness.BLOCKED,
        problems=[readiness.Problem("no_config", "never set up", "`ppy setup`", readiness.RUNTIME)],
    )
    text = readiness.report(verdict)
    assert "Nothing here needs you" in text
