"""A runtime that cannot work has to say so, once, to someone who can act.

The failure this guards is silent, which is what makes it expensive. From Papaya's
side a connected agent whose runtime was never configured looks perfectly healthy:
the connection is live, the listener is running, events are consumed. The first job
starts a harness in a home with no config, produces nothing, and writes a zero-byte
log nobody reads. Observed on a real machine on 2026-09-15.
"""

from __future__ import annotations

import pytest

from papaya_agent_runtime import capabilities, readiness
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


def test_no_registered_repositories_is_a_gap_not_a_block(ppy_home, monkeypatch) -> None:
    """A ticket brings its own repository, so an empty list cannot be fatal.

    `papaya_events.ensure_repository` registers the runtime-owned clone from the
    work item's repository URL the moment the work is picked up. Treating an empty
    list as blocking made a machine that was about to be handed exactly that
    refuse the work — on the first morning of every connection.
    """
    monkeypatch.setattr(readiness, "_harness_problems", lambda problems: None)
    monkeypatch.setattr(readiness, "_papaya_problems", lambda problems: None)
    monkeypatch.setattr(readiness, "_config_problems", lambda problems: None)
    verdict = readiness.check()
    assert verdict.state == readiness.DEGRADED
    assert verdict.blockers == []
    no_repos = next(p for p in verdict.problems if p.code == "no_repos")
    assert no_repos.blocking is False
    # Registering one *ahead of time* is still the user's call, and the fix says
    # both halves: the ticket does it, or they can.
    assert no_repos.owner == readiness.USER
    assert "a work item naming a repository registers it" in no_repos.fix
    assert "`ppy repo add`" in no_repos.fix


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


# ── A harness that is configured but cannot be launched ─────────────────────
#
# Distinct from `no_harness`, and worse, because something *is* signed in here:
# the runtime looks healthy and the dispatch fails only at the moment it matters.
# Switching to whichever harness happens to work would be the runtime overriding a
# choice a person made, so this is theirs to close and it names the sign-in step.


def _harness_report(usable: list[str]) -> dict:
    def make(name: str) -> dict:
        return {
            "name": name,
            "kind": "harness",
            "path": f"/usr/bin/{name}" if name in usable else None,
            "version": "1.0.0",
            "authenticated": name in usable,
            "available": name in usable,
            "detail": "" if name in usable else f"run `{name} login`",
        }

    return {"harnesses": [make("claude"), make("codex")], "requirements": [], "companions": []}


@pytest.fixture
def only_codex_is_signed_in(monkeypatch):
    from papaya_agent_runtime.setup import discovery

    monkeypatch.setattr(discovery, "discover", lambda: _harness_report(["codex"]))


def test_a_configured_harness_that_is_not_usable_blocks_and_needs_the_user(
    ppy_home, monkeypatch, only_codex_is_signed_in
) -> None:
    from papaya_agent_runtime.config import ManagerProfile, MMConfig, WorkerCeiling, save_config

    monkeypatch.setattr(readiness, "_papaya_problems", lambda problems: None)
    monkeypatch.setattr(readiness, "_repo_problems", lambda problems: None)
    save_config(
        MMConfig(
            manager=ManagerProfile("codex", "gpt-5-codex", "high"),
            worker=WorkerCeiling("claude", "opus", "medium"),
        )
    )

    verdict = readiness.check()

    stranded = next(p for p in verdict.problems if p.code == "provider_unusable")
    assert stranded.blocking is True
    assert stranded.owner == readiness.USER
    assert "worker (claude)" in stranded.summary
    assert "claude login" in stranded.fix
    assert verdict.state == readiness.BLOCKED
    assert "no_harness" not in [p.code for p in verdict.problems]


def test_a_configured_harness_that_is_usable_is_not_a_problem(
    ppy_home, monkeypatch, only_codex_is_signed_in
) -> None:
    from papaya_agent_runtime.config import ManagerProfile, MMConfig, WorkerCeiling, save_config

    monkeypatch.setattr(readiness, "_papaya_problems", lambda problems: None)
    monkeypatch.setattr(readiness, "_repo_problems", lambda problems: None)
    save_config(
        MMConfig(
            manager=ManagerProfile("codex", "gpt-5-codex", "high"),
            worker=WorkerCeiling("codex", "gpt-5-codex", "medium"),
        )
    )

    assert [p.code for p in readiness.check().problems] == []


def test_no_signed_in_harness_at_all_says_that_once_not_twice(ppy_home, monkeypatch) -> None:
    """`no_harness` already covers it; adding `provider_unusable` would just be noise."""
    from papaya_agent_runtime.setup import discovery

    monkeypatch.setattr(discovery, "discover", lambda: _harness_report([]))
    monkeypatch.setattr(readiness, "_papaya_problems", lambda problems: None)
    monkeypatch.setattr(readiness, "_repo_problems", lambda problems: None)
    monkeypatch.setattr(readiness, "_config_problems", lambda problems: None)

    codes = [p.code for p in readiness.check().problems]
    assert codes == ["no_harness"]


# ── A stored tool profile that cannot run a repository's gate ───────────────
#
# Every JavaScript worker on 2026-09-16 reported `node --test` denied: the stored
# profile had been copied from a Python-only manager. A stored profile is a choice,
# so it is never rewritten; the person is told what to run.


def _javascript_repo_with_profile(monkeypatch, tools: list[str]) -> None:
    from papaya_agent_runtime import memory, solicit
    from papaya_agent_runtime.config import MMConfig, save_config

    for check in ("_harness_problems", "_papaya_problems", "_repo_problems", "_client_problems"):
        monkeypatch.setattr(readiness, check, lambda problems: None)
    cfg = MMConfig()
    cfg.claude.allowed_tools = tools
    save_config(cfg)
    store.add_repo(
        init_db(),
        name="web",
        origin="https://github.com/acme/web.git",
        local_path="/l",
        default_branch="main",
        base_sha="a" * 40,
        forge_url="https://github.com/acme/web",
    )
    notes = memory.repo_notes_path("web")
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text(
        f"{solicit.NOTES_MARKER}\n# web\n\n## How it builds and verifies\n\n"
        "- test: `node --test`\n\n## Conventions and contracts\n\n- `cp.md` — read first.\n"
        f"{solicit.NOTES_END}\n",
        encoding="utf-8",
    )


def test_a_profile_without_node_warns_for_a_repo_whose_gate_runs_node(
    ppy_home, monkeypatch
) -> None:
    _javascript_repo_with_profile(monkeypatch, ["Read", "Edit", "Bash(git:*)"])
    verdict = readiness.check()
    assert [p.code for p in verdict.problems] == ["claude_tools_lack_gate"]
    problem = verdict.problems[0]
    assert "Bash(node:*) (web)" in problem.summary
    assert problem.fix == "`ppy config claude --reset`"
    assert problem.blocking is False
    assert problem.owner == readiness.USER
    assert verdict.state == readiness.DEGRADED


def test_a_profile_with_node_says_nothing_about_the_gate(ppy_home, monkeypatch) -> None:
    _javascript_repo_with_profile(monkeypatch, ["Read", "Edit", "Bash(git:*)", "Bash(node:*)"])
    assert readiness.check().problems == []


def test_gate_programs_include_what_a_package_manager_needs() -> None:
    commands = ["pnpm install --frozen-lockfile && CI=1 pnpm test", "uv run pytest -q"]
    assert readiness.gate_programs(commands) == {"pnpm", "node", "uv"}


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


# ── The hook that does not depend on being read ─────────────────────────────


def test_a_blocked_runtime_announces_itself_at_session_start(ppy_home, monkeypatch) -> None:
    """The contract's preflight only runs if the session reads the contract.

    A session started non-interactively — the Papaya listener running
    `claude -p "<work item>"` in this directory — arrives with a job and does it.
    On 2026-09-15 three such jobs ran here, none touched `ppy`, and nobody found
    out the runtime had never been set up. A hook is in front of the model
    whatever it was launched to do.
    """
    from papaya_agent_runtime import hooks

    monkeypatch.setattr(readiness, "_harness_problems", lambda problems: None)
    monkeypatch.setattr(readiness, "_papaya_problems", lambda problems: None)

    context = hooks.readiness_context()

    assert context is not None
    assert "RUNTIME READINESS: blocked" in context
    assert "BLOCKS WORK" in context
    # It informs, it does not gate: a session can still answer and help.
    assert "Nothing here is a gate" in context
    assert "`ppy dispatch` has nowhere to run" in context


def test_a_ready_runtime_says_nothing_at_session_start(ppy_home, monkeypatch) -> None:
    """A healthy runtime must not spend context restating that it is healthy."""
    from papaya_agent_runtime import hooks

    monkeypatch.setattr(readiness, "check", lambda: readiness.Readiness(state=readiness.READY))
    assert hooks.readiness_context() is None


def test_the_hook_separates_what_the_agent_fixes_from_what_the_user_must(
    ppy_home, monkeypatch
) -> None:
    from papaya_agent_runtime import hooks

    monkeypatch.setattr(
        readiness,
        "check",
        lambda: readiness.Readiness(
            state=readiness.BLOCKED,
            problems=[
                readiness.Problem("no_config", "never set up", "`ppy setup`", readiness.RUNTIME),
                readiness.Problem("no_harness", "not signed in", "sign in", readiness.USER),
            ],
        ),
    )
    context = hooks.readiness_context()
    assert context is not None
    assert "yours to fix now] never set up" in context
    assert "needs the user] not signed in" in context


def test_a_broken_readiness_check_never_takes_the_session_down(ppy_home, monkeypatch) -> None:
    """A hook that raises would break every session in this directory."""
    from papaya_agent_runtime import hooks
    from papaya_agent_runtime.state import init_db

    def boom() -> None:
        raise RuntimeError("readiness exploded")

    monkeypatch.setattr(readiness, "check", boom)
    hooks.session_start_context(init_db())  # must not raise


def test_a_host_client_newer_than_the_embedded_one_is_a_gap_not_a_block(
    quiet_machine, monkeypatch
) -> None:
    """The app updates its pinned client; the checkout does not. Say so, don't stop.

    The client refuses to delegate on a protocol mismatch, never on a version, so a
    runtime that treated being a release behind as blocking would break machines
    that work.
    """
    monkeypatch.setattr(capabilities, "client_version", lambda: "0.14.0")
    monkeypatch.setenv(capabilities.HOST_CLIENT_VERSION_ENV, "0.15.0")

    verdict = readiness.check()

    assert verdict.state == readiness.DEGRADED
    assert [p.code for p in verdict.problems] == ["client_behind_host"]
    problem = verdict.problems[0]
    assert problem.blocking is False
    assert "0.14.0" in problem.summary and "0.15.0" in problem.summary
    assert problem.fix == "update this checkout and run `uv sync`"


@pytest.mark.parametrize("host", ["0.14.0", "0.13.9", "main", ""])
def test_a_host_client_that_is_not_strictly_newer_says_nothing(
    quiet_machine, monkeypatch, host
) -> None:
    """Equal is the normal case, older is the host's problem, unparseable is unactionable."""
    monkeypatch.setattr(capabilities, "client_version", lambda: "0.14.0")
    monkeypatch.setenv(capabilities.HOST_CLIENT_VERSION_ENV, host)

    verdict = readiness.check()

    assert verdict.state == readiness.READY
    assert verdict.problems == []


def test_every_session_is_told_it_is_the_runtime(ppy_home, monkeypatch) -> None:
    """`ppy start` injects the role; a listener-launched session never goes through it.

    On 2026-09-15 three sessions started by the Papaya listener did the work
    directly in other checkouts — nothing briefed, nothing reviewed at an exact
    commit, nothing delivered through the gate — because all they had was
    CLAUDE.md, which a model holding a work item can reasonably deprioritise.
    """
    from papaya_agent_runtime import hooks

    monkeypatch.delenv("PPY_DEV", raising=False)
    role = hooks.runtime_role_context()
    assert role is not None
    assert "YOU ARE THE PAPAYA AGENT RUNTIME" in role
    assert "ppy dispatch" in role
    assert "skips every gate" in role


def test_a_framework_development_session_is_not_told_it_is_the_runtime(
    ppy_home, monkeypatch
) -> None:
    """PPY_DEV means editing this codebase, not operating it."""
    from papaya_agent_runtime import hooks

    monkeypatch.setenv("PPY_DEV", "1")
    assert hooks.runtime_role_context() is None


def test_the_role_arrives_even_when_the_runtime_is_perfectly_healthy(ppy_home, monkeypatch) -> None:
    """The readiness voice goes quiet once set up; the role must not go with it."""
    from papaya_agent_runtime import hooks
    from papaya_agent_runtime.state import init_db

    monkeypatch.delenv("PPY_DEV", raising=False)
    monkeypatch.setattr(readiness, "check", lambda: readiness.Readiness(state=readiness.READY))
    context = hooks.session_start_context(init_db())
    assert context is not None
    assert "YOU ARE THE PAPAYA AGENT RUNTIME" in context
