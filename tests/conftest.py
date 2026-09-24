"""Shared fixtures for hermetic tests."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import pytest

from papaya_agent_runtime import capabilities, papaya
from papaya_agent_runtime.providers import capability


@pytest.fixture(autouse=True)
def _heartbeat_upkeep_only_where_a_test_asks(request, monkeypatch):
    """The heartbeat's upkeep re-checks readiness, which names this machine's real
    blockers (CI has no signed-in harness); heartbeat tests about other lines should not
    depend on the machine. `tests/test_supervision.py` exercises the real step."""
    if request.module.__name__.endswith("test_supervision"):
        return
    from papaya_agent_runtime import watch

    monkeypatch.setattr(watch.UpkeepStep, "__call__", lambda self, now: [])


@pytest.fixture(autouse=True)
def _session_start_remedies_only_where_a_test_asks(request, monkeypatch):
    """The session-start hook runs serve's start remedies when no serve is running
    (`hooks.start_remedies_context`); hook tests about other context should not see
    first-run setup. `tests/test_supervision.py` exercises the real step."""
    if request.module.__name__.endswith("test_supervision"):
        return
    from papaya_agent_runtime import hooks

    monkeypatch.setattr(hooks, "start_remedies_context", lambda: None)


@pytest.fixture(autouse=True)
def _parity_gaps_are_recorded_only_where_a_test_asks(request, monkeypatch):
    """Every serve start and session start records the open parity gaps as a deficiency
    (`parity.record_gaps`); tests about other deficiencies should not count that entry.
    `tests/test_mode_parity.py` exercises the real recording."""
    if request.module.__name__.endswith("test_mode_parity"):
        return
    from papaya_agent_runtime import parity

    monkeypatch.setattr(parity, "record_gaps", lambda: False)


@pytest.fixture(autouse=True)
def _outreach_reaches_nobody_unless_a_test_asks(request, monkeypatch):
    """The outreach procedure (`outreach.py`) runs from the heartbeat, the hooks and
    serve's rounds and reaches a person through the workspace and the desktop; tests
    about other lines must neither post anywhere nor raise a notification on the
    developer's screen. `tests/test_outreach.py` fakes the channels itself.

    What a process learned about the owner-DM route (a 404, the lines it logged) is
    forgotten between tests, as it is between starts."""
    from papaya_agent_runtime import outreach

    outreach.forget_owner_dm()
    if request.module.__name__.endswith("test_outreach"):
        return
    monkeypatch.setattr(outreach, "post_dm", lambda text, **_: False)
    monkeypatch.setattr(outreach, "post_ticket", lambda item, body, environ=None: False)
    monkeypatch.setattr(outreach, "notify_desktop", lambda text: False)


@pytest.fixture(autouse=True)
def _force_git_lease_backend(monkeypatch):
    """Keep the hermetic suite on the git lease backend.

    The treehouse binary may be installed on the dev machine; without this, the
    supervisor's auto-selected backend would mutate the global ~/.treehouse pool.
    The opt-in live test constructs ``LeaseManager(backend="treehouse")``
    explicitly, which overrides this env.
    """
    monkeypatch.setenv("PPY_LEASE_BACKEND", "git")


@pytest.fixture(autouse=True)
def _git_never_leaves_the_machine(monkeypatch):
    """Git in the suite may only reach local repositories.

    Registration clones from the forge (task 280). A test that named a GitHub URL
    without faking it cloned the real repository on a developer machine whose git
    could reach it, and failed only on CI, which cannot. Refusing every protocol but
    `file` (local paths count as `file`) makes that fail everywhere.
    `tests/test_repos.py::fake_forge` points a GitHub URL at a local repository.
    """
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")


@pytest.fixture(autouse=True)
def _fixture_repos_are_their_own_forge(request, monkeypatch):
    """A hermetic fixture repo's forge is the local source repo it was cloned from.

    ``ppy repo add`` refuses a path with no forge remote, because a repo with
    nowhere to open a pull request is a delivery that strands. Temp git repos on
    disk have no origin at all, so every test that registers one would have to
    invent a GitHub URL — and then delivery would try to push to github.com. So
    the fixtures register the source path as their own forge: no second remote is
    created, and push/PR resolution stays local and offline.

    Tests of the registration rule itself carry ``@pytest.mark.strict_forge`` and
    get the real function.
    """
    if "strict_forge" in request.keywords:
        return
    from papaya_agent_runtime import repos

    real = repos.add_repo

    def add_repo(url_or_path, name=None, forge_url=None):
        return real(url_or_path, name=name, forge_url=forge_url or url_or_path)

    monkeypatch.setattr(repos, "add_repo", add_repo)


@pytest.fixture(autouse=True)
def _hermetic_dispatch_defaults_to_fake(request, monkeypatch):
    """A hermetic dispatch that names no provider means the fake one.

    In production an unnamed provider resolves to the configured worker ceiling
    (``config.default_worker_provider``) and is never ``fake`` — defaulting to
    ``fake`` is what pushed a stub branch to a live remote on 2026-09-04 (issue
    #49). The hermetic suite has no config and wants the deterministic local
    worker, so the default is substituted here, in the tests, rather than left as
    a fallback in the shipped code where a real dispatch could land on it.

    Tests of the resolution rule itself carry ``@pytest.mark.real_provider_default``
    and see the real function.
    """
    if "real_provider_default" in request.keywords:
        return
    from papaya_agent_runtime import config

    monkeypatch.setattr(config, "default_worker_provider", lambda: "fake")


@pytest.fixture(autouse=True)
def _isolated_treehouse_home(tmp_path, monkeypatch):
    """Keep the pool scan off the developer's real treehouse pool.

    ``ppy worktree list`` walks the treehouse pool roots of every registered repo,
    and ``prune`` deletes what it finds there. A test registering a repo whose
    name happened to prefix a real pool directory would otherwise put the
    developer's own worktrees in range.
    """
    monkeypatch.setenv("PPY_TREEHOUSE_HOME", str(tmp_path / "treehouse-home"))


@pytest.fixture(autouse=True)
def _isolated_capability_record(tmp_path, monkeypatch):
    """Keep the capability gate off the developer's machine-local probe record.

    ``.ppy/provider-capabilities.json`` overrides the tracked matrix per provider,
    and ``PPY_HOME`` falls back to the cwd — so on a developer machine that has run
    `make probe`, any test not requesting ``ppy_home`` would read that real record
    and assert against whatever this laptop happens to prove. Point the override
    at an empty temp home so the hermetic suite sees only the tracked matrix.
    """
    monkeypatch.setenv("PPY_HOME", str(tmp_path / "capability-home"))
    capability.clear_cache()
    yield
    capability.clear_cache()


@pytest.fixture(autouse=True)
def _no_real_papaya_connection(tmp_path_factory, monkeypatch):
    """Keep the hermetic suite off this machine's real Papaya connection.

    The runtime discovers a connection wherever one was made — a terminal's
    `~/.papaya-agent`, or the desktop app's own directory — which means that
    without this, every test reads whichever agent the developer happens to be
    connected as. Three pull-request-body tests started asserting against a real
    handle the moment discovery landed. Point it at an empty directory so a test
    sees "not connected" unless it says otherwise, and so CI and a laptop agree.
    """
    monkeypatch.setenv(
        papaya.HOME_ENV, str(tmp_path_factory.mktemp("papaya-client-home", numbered=True))
    )
    # A developer running the suite inside a supervised session inherits the host's
    # client version, which readiness reads. Without this, whether the suite sees a
    # `client_behind_host` warning depends on how the shell was launched.
    monkeypatch.delenv(capabilities.HOST_CLIENT_VERSION_ENV, raising=False)
    # Likewise a shell that set its own sweep cadence: `serve.parse_args` reads it,
    # and a test of the default must not depend on the developer's environment.
    monkeypatch.delenv("PPY_SWEEP_INTERVAL", raising=False)
    # A manager turn the runtime launched carries `PPY_MANAGER_TURN`, which
    # `hooks._headless_turn` reads to keep a headless turn out of the owed and ledger
    # lanes. Inherited by the suite, it silently turned off the owed stop reasons:
    # `test_stop_blocks_once_when_work_is_open_and_no_next_step` failed on a laptop
    # inside a supervised session while CI, which has no such env, stayed green.
    monkeypatch.delenv("PPY_MANAGER_TURN", raising=False)
    monkeypatch.delenv("PPY_MANAGER_SESSION", raising=False)


class HealthyMachine:
    """`readiness.machine` on a machine with nothing wrong: gh signed in, Docker up, disk free.

    A test that needs something broken subclasses it or sets its fields: ``missing``
    (programs `which` cannot find), ``failing`` (argv prefixes that exit 1),
    ``answers`` (argv prefix -> output), ``free`` and ``files``. ``calls`` records
    every command run, including the stdin it was given.
    """

    def __init__(self) -> None:
        self.missing: set[str] = set()
        self.failing: list[tuple[str, ...]] = []
        self.answers: dict[tuple[str, ...], str] = {}
        self.free = 500 * 1024**3
        self.files: set[str] = set()
        self.platform = "darwin"
        self.calls: list[tuple[list[str], str | None]] = []

    def which(self, name: str) -> str | None:
        return None if name in self.missing else f"/usr/local/bin/{name}"

    def run(self, argv, timeout: float = 20.0, input: str | None = None) -> tuple[int, str]:
        self.calls.append((list(argv), input))
        if argv and argv[0] in self.missing:
            return 127, ""
        for prefix in self.failing:
            if tuple(argv[: len(prefix)]) == prefix:
                return 1, ""
        for prefix, answer in self.answers.items():
            if tuple(argv[: len(prefix)]) == prefix:
                return 0, answer
        return 0, ""

    def free_bytes(self, path: str) -> int | None:
        return self.free

    def is_file(self, path: str) -> bool:
        return path in self.files


@pytest.fixture(autouse=True)
def machine(monkeypatch) -> HealthyMachine:
    """Keep readiness's machine checks off the real machine (task 270).

    Readiness now runs `gh auth status`, `docker info` and reads the free disk.
    Unfaked, the suite would depend on whether the developer's `gh` is signed in
    and would call GitHub on every readiness check.
    """
    from papaya_agent_runtime import readiness

    fake = HealthyMachine()
    monkeypatch.setattr(readiness, "machine", fake)
    return fake


#: One of each thing no blocker surface may ever carry (task 270; the same fixture
#: holds task 268's surfaces). A token, a home path, an email address, a diff hunk.
PRIVACY_LEAKS = {
    "token": "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
    "home": "/Users/octo-private/code/secret-project",
    "email": "owner.private@example.com",
    "diff": "diff --git a/app.py b/app.py\n@@ -1,2 +1,2 @@\n-SECRET_OLD = 1\n+SECRET_NEW = 2",
}


@pytest.fixture
def privacy_leaks() -> dict[str, str]:
    """:data:`PRIVACY_LEAKS`, and the fragments that must not survive redaction."""
    return dict(PRIVACY_LEAKS)


def leaked(text: str) -> list[str]:
    """Which of the privacy fixture's fragments ``text`` still carries."""
    fragments = [
        PRIVACY_LEAKS["token"],
        "octo-private",
        PRIVACY_LEAKS["email"],
        "SECRET_OLD",
        "SECRET_NEW",
        "@@ -1,2",
    ]
    return [fragment for fragment in fragments if fragment in text]


@pytest.fixture(autouse=True)
def _no_real_self_reports(monkeypatch):
    """Keep the hermetic suite from opening issues on the runtime's real repository.

    `ppy serve` opens a GitHub issue for every deficiency it records, through `gh`,
    on the origin of this checkout — which, in a developer's clone, is the runtime's
    real repository. A test that trips a signal must not file it there. So `gh`
    answers every call with a failure and the origin reads as not GitHub; a test of
    self-reporting hands its `Reporter` a fake `gh` and an origin of its own.
    """
    from papaya_agent_runtime import deficiencies

    monkeypatch.setattr(
        deficiencies, "run_gh", lambda args, stdin=None: (1, "", "no gh in the hermetic suite")
    )
    monkeypatch.setattr(deficiencies, "origin_url", lambda: None)


@pytest.fixture(autouse=True)
def _no_real_forge_in_reclaim(monkeypatch):
    """Keep worktree reclamation from asking the real forge about a pull request.

    A delivered task's slot is kept while its pull request is open (task 288), and a
    record older than one round is refreshed with `gh`. Unfaked, any test that
    delivers with a PR URL and then prunes would call GitHub. The forge answers "no
    pull request for this branch" here; a test of the rule sets `reclaim.lookup_pr`.
    """
    from papaya_agent_runtime.worktree import reclaim

    monkeypatch.setattr(
        reclaim, "lookup_pr", lambda branch, cwd: {"known": True, "pr": None, "ci": "none"}
    )
    monkeypatch.setattr(reclaim, "_asked", {})


@pytest.fixture(autouse=True)
def _no_supervisor_outlives_its_test(monkeypatch):
    """Stop every supervisor a test created before the test's environment is undone.

    A supervisor's threads find their database through ``PPY_HOME`` each time they
    touch it. A test that returned while its worker was still running — routine on a
    two-core CI runner — left those threads to finish against the *next* test's
    instance, where task ids restart at 1: an old supervisor resumed, deferred or
    released the new test's work, and the new test timed out waiting for a state
    that had already been taken from it (task 259). Requesting ``monkeypatch`` here
    is what orders this teardown before ``PPY_HOME`` is restored.
    """
    from papaya_agent_runtime.supervisor.core import Supervisor
    from papaya_agent_runtime.supervisor.server import SupervisorServer

    supervisors: list = []
    servers: list = []

    def tracked(cls, into):
        real = cls.__init__

        def __init__(self, *args, **kwargs):
            real(self, *args, **kwargs)
            into.append(self)

        monkeypatch.setattr(cls, "__init__", __init__)

    tracked(Supervisor, supervisors)
    tracked(SupervisorServer, servers)
    yield
    for server in servers:
        server.stop()
    stuck = []
    for supervisor in supervisors:
        stuck.extend(supervisor.close(timeout=scale(30)))
    if stuck:
        report = state_dump(f"supervisor threads to stop: {[t.name for t in stuck]}")
        path = _write_dump("supervisor threads to stop", report)
        pytest.fail(f"supervisor threads still running after the test (dump: {path})\n{report}")


@pytest.fixture
def ppy_home(tmp_path, monkeypatch):
    """Point .ppy state at an isolated temp dir for the test."""
    home = tmp_path / ".ppy"
    monkeypatch.setenv("PPY_HOME", str(home))
    return home


def make_git_repo(path) -> str:
    path.mkdir(parents=True, exist_ok=True)

    def run(*args: str) -> None:
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)

    run("init", "-q", "-b", "main")
    run("config", "user.name", "Tester")
    run("config", "user.email", "tester@example.com")
    (path / "README.md").write_text("# fixture\n")
    run("add", ".")
    run("commit", "-qm", "init")
    return str(path)


@pytest.fixture
def source_repo(tmp_path):
    return make_git_repo(tmp_path / "source")


#: How much longer a poll may wait than it would on a developer's laptop.
#:
#: Every asynchronous assertion in this suite is a poll with a deadline: it returns
#: the instant its predicate is true, so a generous deadline costs nothing on a
#: green run and only makes a genuine failure slower to report. On a two-core CI
#: runner the same suite takes roughly twice as long as it does locally, which is
#: how three unrelated supervisor tests failed on the first CI run — different ones
#: on each Python version, none of them actually broken. One knob scales every
#: deadline rather than fifteen hand-tuned numbers drifting apart.
TIMEOUT_SCALE = float(os.environ.get("PPY_TEST_TIMEOUT_SCALE", "1") or "1")


def scale(seconds: float) -> float:
    """A polling deadline, stretched for slower machines."""
    return seconds * TIMEOUT_SCALE


#: Where a deadline that expired leaves its account of the world. Inside the checkout
#: and excluded from git, so a CI failure's dump sits next to the log that printed it.
EVIDENCE_DIR = Path(
    os.environ.get("PPY_TEST_EVIDENCE_DIR")
    or Path(__file__).resolve().parent.parent / ".mm-evidence" / "test-timeouts"
)


def wait_until(predicate, timeout: float, *, what: str = "condition", interval: float = 0.05):
    """Poll ``predicate`` until it is truthy and return its value, or fail explaining why.

    A deadline that expires on a worker lifecycle test used to say only "timed out
    waiting" — nothing about whether the worker spawned, what it last said, or what
    the supervisor's threads were doing. Now it writes all of that to
    :data:`EVIDENCE_DIR` and puts it in the failure, so the next flake explains itself.
    """
    deadline = time.monotonic() + scale(timeout)
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            break
        time.sleep(interval)
    raise timed_out(what)


def timed_out(what: str) -> AssertionError:
    """The failure for an expired wait: the state dump, written and in the message.

    For a poll that cannot use :func:`wait_until` — an async one must await its sleep,
    or it stalls the event loop it is waiting on.
    """
    report = state_dump(what)
    path = _write_dump(what, report)
    return AssertionError(f"timed out waiting for {what} (dump: {path})\n{report}")


def state_dump(what: str) -> str:
    """Everything a stuck worker test needs to be diagnosed after the fact."""
    sections = [f"# timed out waiting for {what}", f"PPY_HOME={os.environ.get('PPY_HOME')}"]
    sections.append(_dump_database())
    sections.append(_dump_files())
    sections.append(_dump_threads())
    sections.append(_dump_processes())
    return "\n\n".join(sections)


def _dump_database() -> str:
    from papaya_agent_runtime.paths import db_path

    path = db_path()
    if not path.exists():
        return f"## database\n(no database at {path})"
    lines = [f"## database {path}"]
    try:
        # Read-only and without init_db: the dump must not take the write lock it may
        # be trying to explain.
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            for table, order in (("tasks", "id"), ("runners", "rowid"), ("events", "id")):
                lines.append(f"### {table}")
                try:
                    rows = conn.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
                except sqlite3.Error as exc:
                    lines.append(f"(could not read: {exc!r})")
                    continue
                lines.extend(json.dumps(dict(row), default=str)[:1500] for row in rows)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        lines.append(f"(could not read: {exc!r})")
    return "\n".join(lines)


def _dump_files() -> str:
    from papaya_agent_runtime.paths import run_dir, runs_dir

    lines = ["## spools and supervisor log"]
    candidates = sorted(runs_dir().glob("**/events.jsonl")) if runs_dir().exists() else []
    log = run_dir() / "supervisor.log"
    if log.exists():
        candidates.append(log)
    if not candidates:
        lines.append("(none)")
    for file in candidates:
        lines.append(f"### {file}")
        text = file.read_text(encoding="utf-8", errors="replace")
        lines.append(text[-8000:])
    return "\n".join(lines)


def _dump_threads() -> str:
    names = {t.ident: f"{t.name} (daemon={t.daemon})" for t in threading.enumerate()}
    lines = ["## threads"]
    for ident, frame in sys._current_frames().items():
        lines.append(f"### {names.get(ident, ident)}")
        lines.append("".join(traceback.format_stack(frame)).rstrip())
    return "\n".join(lines)


def _dump_processes() -> str:
    lines = [f"## processes (cpu_count={os.cpu_count()}, loadavg={_loadavg()})"]
    try:
        out = subprocess.run(
            ["ps", "-o", "pid,ppid,stat,etime,pcpu,command", "-g", str(os.getpgrp())],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        out = f"(ps failed: {exc!r})"
    lines.append(out.rstrip())
    return "\n".join(lines)


def _loadavg() -> str:
    try:
        return " ".join(f"{x:.2f}" for x in os.getloadavg())
    except OSError:
        return "unknown"


def _write_dump(what: str, report: str) -> Path | None:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", os.environ.get("PYTEST_CURRENT_TEST", what))[:120]
    path = EVIDENCE_DIR / f"{time.strftime('%Y%m%dT%H%M%S')}-{slug.strip('-')}.txt"
    try:
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(report, encoding="utf-8")
    except OSError:
        return None
    return path


# --------------------------------------------------------------------------- #
# Pull request descriptions: delivery refuses a head nobody described
# --------------------------------------------------------------------------- #

#: A description that passes `pr_body.validate_description`, for tests about
#: something else in delivery (URLs, stacks, forges) that still has to deliver.
PR_DESCRIPTION = """## Summary

A test change that does one visible thing, described the way a reviewer would.

## Why

Delivery refuses a head nobody described, so a test that delivers describes it.

## Product impact

Nothing reaches a user; this stands in for a real change's effect on people.

## How to test

1. Deliver the task in the test. 2. The pull request opens with this body.

## Risks and what was not verified

None beyond what the test itself asserts; this text is a fixture, not a claim.
"""


def describe(task_id: int, head: str) -> None:
    """Record :data:`PR_DESCRIPTION` for ``task_id`` at ``head``, as approval does."""
    from papaya_agent_runtime import pr_body
    from papaya_agent_runtime.state import init_db

    pr_body.record_description(task_id, head, PR_DESCRIPTION, conn=init_db())


def approve_with_description(task_id: int, findings: str = "", **kwargs) -> dict:
    """What `ppy review approve --pr-description` does: approve, then describe that head."""
    from papaya_agent_runtime import review

    result = review.record_review(task_id, "approved", findings, **kwargs)
    describe(task_id, result["head_sha"])
    return result
