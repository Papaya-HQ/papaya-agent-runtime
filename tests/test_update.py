"""`./bin/ppy update`, the six-hourly update check and its one-line notices (task 396).

The owner asked (2026-09-24) for one command that keeps a runtime current and for a
word when there is something new. Git is faked through `update.run_git`'s seam
(:class:`FakeGit`) for every case; one test runs the real git against local
repositories to prove the commands themselves do what the fake assumes.
"""

from __future__ import annotations

import asyncio
import io
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from papaya_agent_runtime import envsync, outreach, papaya_events, takeover, update

HEAD = "a" * 40
UPSTREAM = "b" * 40
NEWER = "c" * 40
ROOT = "/checkout"


@dataclass
class FakeGit:
    """The git answers `update` reads, one field per question; every call is recorded."""

    remotes: str = "origin"
    origin_head: str | None = "origin/main"
    branch: str | None = "main"
    status: str = ""
    fetch_code: int = 0
    fetch_err: str = ""
    head: str = HEAD
    upstream: str = UPSTREAM
    ahead: int = 0
    behind: int = 0
    subjects: list[str] = field(default_factory=list)
    merge_code: int = 0
    merge_err: str = ""
    calls: list[list[str]] = field(default_factory=list)
    timeouts: dict[str, float] = field(default_factory=dict)

    def __call__(self, root: str, args: list[str], timeout: float) -> tuple[int, str, str]:
        self.calls.append(list(args))
        self.timeouts[args[0]] = timeout
        match args:
            case ["remote"]:
                return 0, self.remotes + "\n", ""
            case ["symbolic-ref", "--quiet", "--short", "HEAD"]:
                return (0, self.branch + "\n", "") if self.branch else (1, "", "")
            case ["symbolic-ref", "--quiet", "--short", _ref]:
                return (0, self.origin_head + "\n", "") if self.origin_head else (1, "", "")
            case ["status", *_]:
                return 0, self.status, ""
            case ["fetch", *_]:
                return self.fetch_code, "", self.fetch_err
            case ["rev-parse", "HEAD"]:
                return 0, self.head + "\n", ""
            case ["rev-parse", "--verify", "--quiet", _ref]:
                return 0, self.upstream + "\n", ""
            case ["rev-list", "--left-right", "--count", _range]:
                return 0, f"{self.ahead}\t{self.behind}\n", ""
            case ["log", _format, count, _range]:
                return 0, "".join(s + "\n" for s in self.subjects[: int(count[2:])]), ""
            case ["merge", *_]:
                if self.merge_code == 0:
                    self.head = self.upstream
                    self.behind = 0
                return self.merge_code, "", self.merge_err
        raise AssertionError(f"unexpected git call: {args}")

    def ran(self, command: str) -> bool:
        return any(call[0] == command for call in self.calls)

    @property
    def touched_work(self) -> bool:
        """Did anything run that could move or rewrite local work?"""
        return any(
            call[0] in ("merge", "reset", "rebase", "stash", "checkout", "pull")
            for call in self.calls
        )


@dataclass
class Run:
    code: int
    out: list[str]
    err: list[str]
    rebuilt: list[str]


def _update(git: FakeGit, *, serve=None, rebuild_code: int = 0) -> Run:
    out: list[str] = []
    err: list[str] = []
    rebuilt: list[str] = []

    def rebuild(root: str) -> int:
        rebuilt.append(root)
        return rebuild_code

    code = update.run(
        ROOT,
        git=git,
        rebuild=rebuild,
        serve_record=lambda: serve,
        say=out.append,
        refuse=err.append,
    )
    return Run(code, out, err, rebuilt)


# ── ./bin/ppy update: the refusals ──────────────────────────────────────────


def test_a_dirty_checkout_is_refused_naming_up_to_five_files_and_nothing_changes() -> None:
    files = [f"src/f{i}.py" for i in range(7)]
    git = FakeGit(status="".join(f" M {f}\n" for f in files), behind=3)
    run = _update(git)
    assert run.code == 1 and run.out == [] and run.rebuilt == []
    [line] = run.err
    assert (
        "uncommitted changes (src/f0.py, src/f1.py, src/f2.py, src/f3.py, src/f4.py and 2 more)"
        in line
    )
    assert "src/f5.py" not in line
    assert "./bin/ppy update" in line
    assert not git.ran("fetch") and not git.touched_work


def test_a_renamed_file_is_named_by_its_new_path() -> None:
    run = _update(FakeGit(status="R  old.py -> new.py\n"))
    assert "(new.py)" in run.err[0]


@pytest.mark.parametrize(
    ("branch", "said"),
    [("feature", "on branch feature, not main"), (None, "not on a branch (HEAD is detached)")],
)
def test_a_checkout_off_the_default_branch_is_refused(branch, said) -> None:
    git = FakeGit(branch=branch, behind=3)
    run = _update(git)
    assert run.code == 1 and run.out == [] and run.rebuilt == []
    [line] = run.err
    assert said in line and "git checkout main" in line
    assert not git.ran("fetch") and not git.touched_work


def test_local_commits_upstream_does_not_have_are_refused_and_never_reset_or_merged() -> None:
    git = FakeGit(ahead=2, behind=3)
    run = _update(git)
    assert run.code == 1 and run.out == [] and run.rebuilt == []
    [line] = run.err
    assert "2 commit(s) that origin/main does not" in line and "cannot fast-forward" in line
    assert "Nothing was changed" in line
    assert git.ran("fetch") and not git.touched_work


def test_a_failed_fetch_is_refused_in_one_line() -> None:
    git = FakeGit(
        fetch_code=128, fetch_err="fatal: unable to access 'https://x': Could not resolve host"
    )
    run = _update(git)
    assert run.code == 1 and run.out == []
    [line] = run.err
    assert "could not fetch origin/main (fatal: unable to access" in line
    assert not git.touched_work


def test_a_fast_forward_git_refuses_is_reported_and_nothing_is_rebuilt() -> None:
    git = FakeGit(
        behind=2, merge_code=1, merge_err="error: Your local changes would be overwritten"
    )
    run = _update(git)
    assert run.code == 1 and run.rebuilt == []
    assert "git could not fast-forward to origin/main (error: Your local changes" in run.err[0]


def test_no_remote_is_refused() -> None:
    run = _update(FakeGit(remotes=""))
    assert run.code == 1 and "no remote to update from" in run.err[0]


# ── ./bin/ppy update: updating ──────────────────────────────────────────────


def test_three_behind_fast_forwards_lists_the_three_subjects_and_rebuilds_the_environment() -> None:
    git = FakeGit(behind=3, subjects=["Add update", "Fix status", "Docs"])
    run = _update(git)
    assert run.code == 0 and run.err == []
    assert run.out == [
        "Updated aaaaaaa → bbbbbbb (3 changes)",
        "  - Add update",
        "  - Fix status",
        "  - Docs",
        update.START,
    ]
    assert ["merge", "--ff-only", "--quiet", "refs/remotes/origin/main"] in git.calls
    assert run.rebuilt == [ROOT]
    # The fetch is bounded; nothing else waits on the network.
    assert git.timeouts["fetch"] == update.FETCH_TIMEOUT_SECONDS


def test_more_than_five_changes_lists_five_subjects_and_how_many_more() -> None:
    git = FakeGit(behind=7, subjects=[f"change {i}" for i in range(7)])
    run = _update(git)
    assert run.out[0] == "Updated aaaaaaa → bbbbbbb (7 changes)"
    assert run.out[1:6] == [f"  - change {i}" for i in range(5)]
    assert run.out[6] == "  … and 2 more"


def test_one_change_is_singular() -> None:
    run = _update(FakeGit(behind=1, subjects=["Only"]))
    assert run.out[0] == "Updated aaaaaaa → bbbbbbb (1 change)"


def test_already_current_says_up_to_date_and_neither_merges_nor_rebuilds() -> None:
    git = FakeGit(behind=0)
    run = _update(git)
    assert run.code == 0 and run.out == ["Up to date (aaaaaaa)."] and run.rebuilt == []
    assert git.ran("fetch") and not git.touched_work


def test_a_failed_rebuild_fails_the_command_after_the_update() -> None:
    run = _update(FakeGit(behind=1, subjects=["x"]), rebuild_code=1)
    assert run.code == 1 and run.out[0].startswith("Updated ")
    assert update.START not in run.out


@pytest.mark.parametrize(
    ("serve", "line"),
    [
        (None, "Start it with: ./bin/ppy serve"),
        ({"launched_by": "app"}, "Restart to use it: quit and reopen the Papaya app."),
        ({"launched_by": "terminal"}, "Restart to use it: stop it and run ./bin/ppy serve again."),
        ({}, update.RESTART_EITHER),
    ],
    ids=["none running", "desktop app", "terminal", "unknown launcher"],
)
def test_the_restart_line_names_how_the_running_runtime_was_started(serve, line) -> None:
    run = _update(FakeGit(behind=1, subjects=["x"]), serve=serve)
    assert run.out[-1] == line


def test_the_default_rebuild_goes_through_the_stamp_check(tmp_path, monkeypatch) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "uv.lock").write_text("lock 1\n")
    (root / "pyproject.toml").write_text("[project]\n")
    (root / ".python-version").write_text("3.13\n")
    env = tmp_path / "env"
    env.mkdir()
    (env / envsync.STAMP).write_text(envsync.wanted_stamp(str(root), "3.13") + "\n")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(env))
    monkeypatch.setenv("PPY_HOME", str(tmp_path / ".ppy"))
    built: list[str] = []
    monkeypatch.setattr(envsync, "build", lambda root, python, **_: built.append(python) or 0)

    assert update.default_rebuild(str(root)) == 0
    assert built == []  # the lockfile did not change: nothing to rebuild

    (root / "uv.lock").write_text("lock 2\n")  # the pull changed it
    assert update.default_rebuild(str(root)) == 0
    assert built == ["3.13"]


# ── which serve is running, and how it was started ──────────────────────────


def test_serve_json_records_what_launched_it_and_update_reads_it(tmp_path) -> None:
    home = str(tmp_path / ".ppy")
    assert update.running_serve(home) is None
    taken = takeover.take_serve(home, {"launched_by": update.LAUNCHED_BY_APP}, timeout=1.0)
    assert taken.lock is not None
    try:
        assert takeover.read_serve_record(home)["launched_by"] == "app"
        record = update.running_serve(home)
        assert record is not None and update.restart_line(record) == update.RESTART_APP
    finally:
        taken.lock.release()
    assert update.running_serve(home) is None


def test_serve_identity_says_app_when_supervised_and_terminal_otherwise() -> None:
    from papaya_agent_runtime import serve

    assert serve.serve_identity(supervised=True)["launched_by"] == "app"
    assert serve.serve_identity()["launched_by"] == "terminal"


# ── the periodic check and its notice ───────────────────────────────────────

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def test_the_check_fetches_once_per_six_hours_and_owes_one_notice_per_upstream_head(
    tmp_path,
) -> None:
    home = tmp_path / ".ppy"
    git = FakeGit(behind=3)
    notice = update.check_due(NOW, root=ROOT, git=git, home=home)
    assert notice is not None and notice.upstream_sha == UPSTREAM and notice.behind == 3
    assert (
        notice.text
        == "A runtime update is available (3 changes). Run ./bin/ppy update, then restart."
    )
    update.record_notified(notice.upstream_sha, home)

    # Within six hours: not even a fetch.
    fetches = sum(1 for c in git.calls if c[0] == "fetch")
    assert update.check_due(NOW + timedelta(hours=5), root=ROOT, git=git, home=home) is None
    assert sum(1 for c in git.calls if c[0] == "fetch") == fetches

    # Six hours on, the same upstream head: fetched, nothing owed.
    assert update.check_due(NOW + timedelta(hours=6), root=ROOT, git=git, home=home) is None
    assert sum(1 for c in git.calls if c[0] == "fetch") == fetches + 1

    # Upstream moved: a new notice.
    git.upstream, git.behind = NEWER, 4
    again = update.check_due(NOW + timedelta(hours=12), root=ROOT, git=git, home=home)
    assert again is not None and again.upstream_sha == NEWER and again.behind == 4


def test_the_notice_goes_to_the_owner_as_a_notice_keyed_on_the_upstream_sha() -> None:
    message = update.Notice(UPSTREAM, 2).owner_message()
    assert isinstance(message, outreach.OwnerMessage)
    assert message.kind == papaya_events.OWNER_DM_NOTICE
    assert message.dedupe_key == f"runtime-update:{UPSTREAM}"


def test_a_failed_fetch_is_one_log_line_and_no_notice(tmp_path, caplog) -> None:
    home = tmp_path / ".ppy"
    git = FakeGit(behind=3, fetch_code=128, fetch_err="fatal: Could not resolve host: github.com")
    with caplog.at_level(logging.WARNING, logger="papaya_agent_runtime.update"):
        assert update.check_due(NOW, root=ROOT, git=git, home=home) is None
        # Still offline an hour later: no second fetch, no second line.
        assert update.check_due(NOW + timedelta(hours=1), root=ROOT, git=git, home=home) is None
    lines = [r.getMessage() for r in caplog.records if r.name == "papaya_agent_runtime.update"]
    assert lines == [
        "[update] Could not check origin/main for a runtime update: "
        "fatal: Could not resolve host: github.com"
    ]


def test_a_checkout_off_its_default_branch_is_never_checked(tmp_path) -> None:
    git = FakeGit(branch="feature", behind=3)
    assert update.check_due(NOW, root=ROOT, git=git, home=tmp_path) is None
    assert not git.ran("fetch")


def test_the_heartbeat_step_says_it_once_and_again_only_if_it_did_not_land(tmp_path) -> None:
    home = tmp_path / ".ppy"
    git = FakeGit(behind=3)
    said: list[tuple[str, outreach.OwnerMessage]] = []
    landed = [False]

    def say(text: str, *, owner: outreach.OwnerMessage) -> bool:
        said.append((text, owner))
        return landed[0]

    assert update.step(NOW, say=say, root=ROOT, git=git, home=home) == []
    assert len(said) == 1  # tried, did not land: not recorded
    landed[0] = True
    lines = update.step(NOW + timedelta(hours=6), say=say, root=ROOT, git=git, home=home)
    assert lines == ["told the owner a runtime update is available (3 changes)"]
    assert len(said) == 2
    # The same upstream head seen again: never said a second time.
    assert update.step(NOW + timedelta(hours=12), say=say, root=ROOT, git=git, home=home) == []
    assert len(said) == 2


def test_the_heartbeat_runs_the_update_step_when_no_serve_runs(ppy_home, monkeypatch) -> None:
    from papaya_agent_runtime import supervision, watch

    monkeypatch.setattr(supervision, "serve_running", lambda: False)
    monkeypatch.setattr(supervision, "blocker_step", lambda: [])
    monkeypatch.setattr(supervision, "assigned_unpicked", lambda now: [])
    monkeypatch.setattr(supervision, "last_hygiene_at", lambda: NOW)
    monkeypatch.setattr(watch.lanes, "deficiency_step", lambda *a: [])
    seen: list[datetime] = []
    monkeypatch.setattr(update, "step", lambda now, say: seen.append(now) or ["update line"])
    assert "update line" in watch.UpkeepStep()(NOW)
    assert seen == [NOW]


# ── serve's rounds ──────────────────────────────────────────────────────────


def _rounds(api=None):
    from papaya_agent_runtime import rounds

    return rounds.Rounds(
        SimpleNamespace(api=api or object(), standalone=False),
        SimpleNamespace(held={}),
        papaya_env=lambda: {"PAPAYA_AGENT_TOKEN": "t"},
    )


def test_serve_rounds_tell_the_owner_once_per_upstream_head(ppy_home, monkeypatch) -> None:
    git = FakeGit(behind=3)
    monkeypatch.setattr(update, "run_git", git)
    monkeypatch.setattr(update, "checkout_root", lambda: ROOT)
    said: list[tuple[str, outreach.OwnerMessage]] = []

    async def say_in_workspace(api, text, *, owner=None, environ=None, unreached=None):
        said.append((text, owner))
        return True

    monkeypatch.setattr(outreach, "say_in_workspace", say_in_workspace)
    lanes = _rounds()

    first = asyncio.run(lanes._update_lane(NOW))
    assert first == ["told the owner a runtime update is available (3 changes)"]
    assert (
        said[0][0]
        == "A runtime update is available (3 changes). Run ./bin/ppy update, then restart."
    )
    assert said[0][1].dedupe_key == f"runtime-update:{UPSTREAM}"
    # A later check sees the same upstream head: one notice, not two.
    assert asyncio.run(lanes._update_lane(NOW + timedelta(hours=6))) == []
    assert len(said) == 1
    git.upstream, git.behind = NEWER, 5
    assert asyncio.run(lanes._update_lane(NOW + timedelta(hours=12))) != []
    assert len(said) == 2 and said[1][1].dedupe_key == f"runtime-update:{NEWER}"


def test_serve_rounds_go_on_when_the_fetch_fails(ppy_home, monkeypatch, caplog) -> None:
    git = FakeGit(behind=3, fetch_code=128, fetch_err="fatal: offline")
    monkeypatch.setattr(update, "run_git", git)
    monkeypatch.setattr(update, "checkout_root", lambda: ROOT)

    async def never(*a, **k):
        raise AssertionError("nothing is said when the check could not fetch")

    monkeypatch.setattr(outreach, "say_in_workspace", never)
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(_rounds()._update_lane(NOW)) == []
    assert [r.getMessage() for r in caplog.records if "offline" in r.getMessage()] == [
        "[update] Could not check origin/main for a runtime update: fatal: offline"
    ]

    def broken(now):
        raise RuntimeError("disk full")

    monkeypatch.setattr(update, "check_due", broken)
    assert asyncio.run(_rounds()._update_lane(NOW + timedelta(hours=6))) == []


def test_a_notice_that_did_not_land_is_said_at_the_next_check(ppy_home, monkeypatch) -> None:
    monkeypatch.setattr(update, "run_git", FakeGit(behind=2))
    monkeypatch.setattr(update, "checkout_root", lambda: ROOT)
    calls: list[str] = []

    async def unreached(api, text, **_):
        calls.append(text)
        return False

    monkeypatch.setattr(outreach, "say_in_workspace", unreached)
    lanes = _rounds()
    assert asyncio.run(lanes._update_lane(NOW)) == []
    assert asyncio.run(lanes._update_lane(NOW + timedelta(hours=6))) == []
    assert len(calls) == 2 and update.read_state().get("notified_sha") is None


# ── the one line in status and setup ───────────────────────────────────────


def test_the_available_line_reads_the_last_fetch_and_never_fetches() -> None:
    git = FakeGit(behind=3)
    assert update.available_line(ROOT, git) == "Update available (3 changes): ./bin/ppy update"
    assert not git.ran("fetch")
    assert update.available_line(ROOT, FakeGit(behind=0)) is None
    assert update.available_line(ROOT, FakeGit(branch="feature", behind=3)) is None
    assert update.available_line(ROOT, FakeGit(remotes="")) is None


def test_ppy_status_shows_the_line_when_behind(ppy_home, monkeypatch, capsys) -> None:
    from papaya_agent_runtime import cli

    monkeypatch.setenv("PPY_QUIET_INVITE", "1")
    monkeypatch.setattr(update, "run_git", FakeGit(behind=4))
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "Update available (4 changes): ./bin/ppy update" in out

    monkeypatch.setattr(update, "run_git", FakeGit(behind=0))
    assert cli.main(["status"]) == 0
    assert not [line for line in capsys.readouterr().out.splitlines() if "Update available" in line]


def test_ppy_setup_shows_the_line_before_done_when_behind(monkeypatch) -> None:
    from papaya_agent_runtime.setup import guided

    for step in ("machine", "claude", "github", "papaya", "repositories", "profile"):
        monkeypatch.setattr(guided.Setup, step, lambda self: None)
    monkeypatch.setattr(update, "run_git", FakeGit(behind=2))
    out = io.StringIO()
    assert guided.Setup(guided.Options(), env={}, out=out).run() == 0
    lines = out.getvalue().splitlines()
    assert lines[-3:] == [
        "Update available (2 changes): ./bin/ppy update",
        "Done. Start it with:",
        "  ./bin/ppy serve",
    ]

    monkeypatch.setattr(update, "run_git", FakeGit(behind=0))
    out = io.StringIO()
    guided.Setup(guided.Options(), env={}, out=out).run()
    assert "Update available" not in out.getvalue()


# ── the real git ────────────────────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name + "\n")
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", f"Add {name}")


@pytest.fixture
def clones(tmp_path, monkeypatch):
    """A bare remote, the runtime's clone of it, and another clone that pushes to it."""
    for key, value in {
        "GIT_AUTHOR_NAME": "Tester",
        "GIT_AUTHOR_EMAIL": "tester@example.com",
        "GIT_COMMITTER_NAME": "Tester",
        "GIT_COMMITTER_EMAIL": "tester@example.com",
    }.items():
        monkeypatch.setenv(key, value)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True, capture_output=True)
    _git(other, "symbolic-ref", "HEAD", "refs/heads/main")
    _commit(other, "first")
    _git(other, "push", "-q", "origin", "main")
    runtime = tmp_path / "runtime"
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(runtime)], check=True, capture_output=True
    )
    return SimpleNamespace(remote=remote, other=other, runtime=runtime)


def _real_update(root: Path) -> Run:
    out: list[str] = []
    err: list[str] = []
    code = update.run(
        str(root), rebuild=lambda r: 0, serve_record=lambda: None, say=out.append, refuse=err.append
    )
    return Run(code, out, err, [])


def test_real_git_fast_forwards_and_refuses_a_diverged_checkout(clones) -> None:
    for name in ("a", "b", "c"):
        _commit(clones.other, name)
    _git(clones.other, "push", "-q", "origin", "main")
    upstream_head = _git(clones.other, "rev-parse", "HEAD")

    # The last fetch has not seen them yet: status says nothing until a check fetches.
    assert update.available_line(str(clones.runtime)) is None
    run = _real_update(clones.runtime)
    assert run.code == 0, run.err
    assert run.out[0].endswith("(3 changes)")
    assert run.out[1:4] == ["  - Add c", "  - Add b", "  - Add a"]
    assert _git(clones.runtime, "rev-parse", "HEAD") == upstream_head
    assert _real_update(clones.runtime).out == [f"Up to date ({upstream_head[:7]})."]

    # Diverged: a local commit, and a new one upstream.
    _commit(clones.runtime, "local")
    local_head = _git(clones.runtime, "rev-parse", "HEAD")
    _commit(clones.other, "d")
    _git(clones.other, "push", "-q", "origin", "main")
    run = _real_update(clones.runtime)
    assert run.code == 1 and "cannot fast-forward" in run.err[0]
    assert _git(clones.runtime, "rev-parse", "HEAD") == local_head
    # The fetch did happen, so status now knows.
    assert update.available_line(str(clones.runtime)) == (
        "Update available (1 change): ./bin/ppy update"
    )


def test_real_git_refuses_a_dirty_checkout_without_touching_it(clones) -> None:
    _commit(clones.other, "a")
    _git(clones.other, "push", "-q", "origin", "main")
    (clones.runtime / "first").write_text("edited\n")
    head = _git(clones.runtime, "rev-parse", "HEAD")
    run = _real_update(clones.runtime)
    assert run.code == 1 and "uncommitted changes (first)" in run.err[0]
    assert _git(clones.runtime, "rev-parse", "HEAD") == head
    assert (clones.runtime / "first").read_text() == "edited\n"
