"""Bringing this runtime up to date, and hearing that there is something to bring.

Until 2026-09-24 a runtime was updated by hand: `git pull` in the checkout, and a
pull that changed the lockfile crashed the next `ppy setup` until the environment
was rebuilt. Nobody was told a new version existed unless they watched the
repository. The owner asked for both: one command, and a word when it is due.

- `./bin/ppy update` (:func:`run`) fast-forwards this checkout to its remote's
  default branch, rebuilds the environment through the same stamp check every
  `./bin/ppy` command runs (`envsync --before-command`), and says how to restart.
  It refuses, in one line naming the reason and the fix, rather than touch local
  work: uncommitted changes, another branch checked out, or commits upstream does
  not have. It never resets, merges, stashes or restarts anything.
- A running `ppy serve` (its rounds) and a session's heartbeat, when no serve runs,
  fetch the default branch every :data:`CHECK_EVERY_SECONDS` (:func:`check_due`),
  and say "an update is available" to the owner once per upstream head, through the
  same outreach path everything else they are told takes.
- `ppy status` and `ppy setup` show one line when the last fetch left this checkout
  behind (:func:`available_line`). Neither fetches: they read the remote-tracking
  ref, which is what the last fetch left.

Every git call goes through :func:`run_git`, the one test seam.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: How often the running runtime asks its remote whether there is anything new.
CHECK_EVERY_SECONDS = 6 * 60 * 60.0

#: A fetch that has not finished by then is abandoned; the next check tries again.
FETCH_TIMEOUT_SECONDS = 60.0

#: Every other git call reads local refs only.
LOCAL_TIMEOUT_SECONDS = 20.0

#: How many incoming commit subjects an update lists, and how many dirty files a refusal names.
SUBJECTS = 5
NAMED_FILES = 5

COMMAND = "./bin/ppy update"
SERVE = "./bin/ppy serve"

#: Where the check keeps when it last fetched and which upstream head it last told the owner of.
STATE_FILE = "update.json"

#: Papaya dedupes an owner-DM message on its key; one key per upstream head.
NOTICE_KEY_PREFIX = "runtime-update:"

#: How serve.json names who started a `serve` (`serve.serve_identity`).
LAUNCHED_BY_APP = "app"
LAUNCHED_BY_TERMINAL = "terminal"

RESTART_APP = "Restart to use it: quit and reopen the Papaya app."
RESTART_TERMINAL = f"Restart to use it: stop it and run {SERVE} again."
RESTART_EITHER = (
    "Restart to use it: quit and reopen the Papaya app, or, if it was started in a "
    f"terminal, stop it and run {SERVE} again."
)
START = f"Start it with: {SERVE}"

#: Variables that point git at another repository (a git hook sets them).
_REDIRECTS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")

#: ``(root, args, timeout) -> (exit code, stdout, stderr)``.
GitRunner = Callable[[str, list[str], float], tuple[int, str, str]]


def run_git(root: str, args: list[str], timeout: float) -> tuple[int, str, str]:
    """``git -C root args``; never raises. A git that is missing or hangs is exit -1."""
    env = {key: value for key, value in os.environ.items() if key not in _REDIRECTS}
    try:
        proc = subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"git {args[0]} took longer than {timeout:g}s"
    except OSError as exc:
        return -1, "", str(exc)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _git(git: GitRunner | None) -> GitRunner:
    # Resolved at call time, so a test that replaces `run_git` reaches every caller.
    return git or run_git


def _out(git: GitRunner, root: str, *args: str) -> str | None:
    code, out, _err = git(root, list(args), LOCAL_TIMEOUT_SECONDS)
    return out.strip() if code == 0 else None


def _error(err: str, out: str = "") -> str:
    lines = [line.strip() for line in (err or out).splitlines() if line.strip()]
    said = next((line for line in lines if line.lower().startswith(("fatal:", "error:"))), "")
    said = said or (lines[-1] if lines else "no reason given")
    return said if len(said) <= 160 else said[:159] + "…"


def checkout_root() -> str:
    from papaya_agent_runtime import readiness

    return str(readiness.checkout_root())


def _changes(count: int) -> str:
    return f"{count} change" + ("" if count == 1 else "s")


# ── where upstream is ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Upstream:
    """The remote and default branch this checkout updates from."""

    remote: str
    branch: str

    @property
    def ref(self) -> str:
        return f"refs/remotes/{self.remote}/{self.branch}"

    @property
    def name(self) -> str:
        return f"{self.remote}/{self.branch}"


def upstream(root: str, git: GitRunner | None = None) -> Upstream | None:
    """``origin`` (or the only remote) and its default branch; None with no remote.

    The default branch is the remote's HEAD as the clone recorded it, else ``main``,
    else ``master`` when only that exists: read locally, no network.
    """
    git = _git(git)
    remotes = (_out(git, root, "remote") or "").split()
    if not remotes:
        return None
    remote = "origin" if "origin" in remotes else remotes[0]
    head = _out(git, root, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD")
    if head and head.startswith(f"{remote}/"):
        return Upstream(remote, head.removeprefix(f"{remote}/"))
    for branch in ("main", "master"):
        found = _out(
            git, root, "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{branch}"
        )
        if found:
            return Upstream(remote, branch)
    return Upstream(remote, "main")


def fetch(root: str, up: Upstream, git: GitRunner | None = None) -> str | None:
    """One fetch of the default branch into its remote-tracking ref; the error, or None."""
    code, out, err = _git(git)(
        root,
        ["fetch", "--quiet", up.remote, f"+refs/heads/{up.branch}:{up.ref}"],
        FETCH_TIMEOUT_SECONDS,
    )
    return None if code == 0 else _error(err, out)


def current_branch(root: str, git: GitRunner | None = None) -> str | None:
    """The checked-out branch; None when HEAD is detached."""
    return _out(_git(git), root, "symbolic-ref", "--quiet", "--short", "HEAD") or None


@dataclass(frozen=True)
class Position:
    """Where HEAD stands against the remote-tracking ref, as the last fetch left it."""

    head: str
    upstream: str
    #: Commits upstream has that HEAD does not: what an update brings.
    behind: int
    #: Commits HEAD has that upstream does not: what stops a fast-forward.
    ahead: int


def position(root: str, up: Upstream, git: GitRunner | None = None) -> Position | None:
    git = _git(git)
    head = _out(git, root, "rev-parse", "HEAD")
    theirs = _out(git, root, "rev-parse", "--verify", "--quiet", f"{up.ref}^{{commit}}")
    if not head or not theirs:
        return None
    counts = (
        _out(git, root, "rev-list", "--left-right", "--count", f"HEAD...{up.ref}") or ""
    ).split()
    if len(counts) != 2 or not all(c.isdigit() for c in counts):
        return None
    return Position(head=head, upstream=theirs, ahead=int(counts[0]), behind=int(counts[1]))


def behind(root: str | None = None, git: GitRunner | None = None) -> Position | None:
    """This checkout's position when it is on the default branch and behind it; else None.

    Local refs only. A checkout on another branch (somebody developing the runtime)
    is not told to update: `ppy update` would refuse it.
    """
    root = root or checkout_root()
    up = upstream(root, git)
    if up is None or current_branch(root, git) != up.branch:
        return None
    found = position(root, up, git)
    return found if found is not None and found.behind > 0 else None


def available_line(root: str | None = None, git: GitRunner | None = None) -> str | None:
    """ "Update available (N changes): ./bin/ppy update", from the last fetch; never raises."""
    try:
        found = behind(root, git)
    except Exception:  # noqa: BLE001 - a status line must never break status
        return None
    if found is None:
        return None
    return f"Update available ({_changes(found.behind)}): {COMMAND}"


# ── ./bin/ppy update ────────────────────────────────────────────────────────


def dirty_files(root: str, git: GitRunner | None = None) -> list[str] | None:
    """Tracked files with uncommitted changes; None when git could not say.

    Untracked files are not local work a fast-forward can lose: one that an
    incoming commit would overwrite makes git refuse the fast-forward itself.
    """
    code, out, _err = _git(git)(
        root, ["status", "--porcelain", "--untracked-files=no"], LOCAL_TIMEOUT_SECONDS
    )
    if code != 0:
        return None
    files = []
    for line in out.splitlines():
        if len(line) > 3:
            files.append(line[3:].split(" -> ")[-1].strip().strip('"'))
    return files


def _named(files: list[str]) -> str:
    shown = ", ".join(files[:NAMED_FILES])
    more = len(files) - NAMED_FILES
    return shown + (f" and {more} more" if more > 0 else "")


def running_serve(home: str | None = None) -> dict[str, Any] | None:
    """The running `serve`'s ``serve.json`` (``{}`` if it wrote none); None if none runs.

    Read without taking the lock: the pid the lock file names must still be the
    process that took it (`takeover.LockHolder.is_running`).
    """
    from papaya_agent_runtime import takeover
    from papaya_agent_runtime.paths import ppy_home

    home = home or str(ppy_home().resolve())
    holder = takeover.read_lock_holder(home)
    if holder is None or not holder.is_running(takeover.process_started):
        return None
    return takeover.read_serve_record_of(home, holder.pid) or {}


def restart_line(record: Mapping[str, Any] | None) -> str:
    """How to put the new code to use, given the running `serve` (None: nothing runs)."""
    if record is None:
        return START
    launched_by = record.get("launched_by")
    if launched_by == LAUNCHED_BY_APP:
        return RESTART_APP
    if launched_by == LAUNCHED_BY_TERMINAL:
        return RESTART_TERMINAL
    # A serve started before serve.json said who launched it.
    return RESTART_EITHER


def default_rebuild(root: str) -> int:
    """The environment rebuild every `./bin/ppy` command runs first, after the pull.

    `envsync --before-command` compares the environment's stamp with uv.lock and
    pyproject.toml: nothing happens when they did not change, one line and a rebuild
    when they did, and nothing is rebuilt under a running `serve` (its next start does).
    """
    from papaya_agent_runtime import envsync

    python = envsync.pinned_python(root)
    return envsync.main(
        ["--project", root, "--before-command", *(["--python", python] if python else [])]
    )


def run(
    root: str | None = None,
    *,
    git: GitRunner | None = None,
    rebuild: Callable[[str], int] | None = None,
    serve_record: Callable[[], Mapping[str, Any] | None] | None = None,
    say: Callable[[str], None] | None = None,
    refuse: Callable[[str], None] | None = None,
) -> int:
    """`./bin/ppy update`: 0 updated or already current, 1 refused or failed.

    Refusals are one line on ``refuse`` (stderr) and change nothing.
    """
    root = root or checkout_root()
    git = _git(git)
    say = say or print
    refuse = refuse or _stderr
    rebuild = rebuild or default_rebuild
    serve_record = serve_record or running_serve

    def refused(why: str) -> int:
        refuse(f"ppy update: not updated: {why}")
        return 1

    up = upstream(root, git)
    if up is None:
        return refused(
            "this checkout has no remote to update from. Add one (`git remote add origin <url>`) "
            f"and run {COMMAND} again."
        )
    dirty = dirty_files(root, git)
    if dirty is None:
        return refused(f"git could not read this checkout's status at {root}.")
    if dirty:
        return refused(
            f"this checkout has uncommitted changes ({_named(dirty)}). Commit or stash them, "
            f"then run {COMMAND} again."
        )
    branch = current_branch(root, git)
    if branch != up.branch:
        where = f"on branch {branch}" if branch else "not on a branch (HEAD is detached)"
        return refused(
            f"this checkout is {where}, not {up.branch}. Switch with `git checkout {up.branch}`, "
            f"then run {COMMAND} again."
        )
    failed = fetch(root, up, git)
    if failed:
        return refused(
            f"could not fetch {up.name} ({failed}). Check the network and run {COMMAND} again."
        )
    found = position(root, up, git)
    if found is None:
        return refused(f"git could not compare {branch} with {up.name}.")
    if found.behind == 0:
        say(f"Up to date ({found.head[:7]}).")
        return 0
    if found.ahead:
        return refused(
            f"{branch} has {found.ahead} commit(s) that {up.name} does not, so it cannot "
            f"fast-forward. Nothing was changed; move them to another branch "
            f"(`git branch <name>`, then `git reset --keep {up.name}`) and run {COMMAND} again."
        )
    code, out, err = git(
        root, ["log", "--format=%s", f"-n{SUBJECTS}", f"HEAD..{up.ref}"], LOCAL_TIMEOUT_SECONDS
    )
    subjects = [line for line in out.splitlines() if line.strip()] if code == 0 else []
    code, out, err = git(root, ["merge", "--ff-only", "--quiet", up.ref], LOCAL_TIMEOUT_SECONDS)
    if code != 0:
        return refused(
            f"git could not fast-forward to {up.name} ({_error(err, out)}). Nothing was "
            f"changed; resolve that and run {COMMAND} again."
        )
    say(f"Updated {found.head[:7]} → {found.upstream[:7]} ({_changes(found.behind)})")
    for subject in subjects:
        say(f"  - {subject}")
    if found.behind > len(subjects) and subjects:
        say(f"  … and {found.behind - len(subjects)} more")
    if rebuild(root) != 0:
        # envsync has said why in one line and named `./bin/ppy env sync`.
        return 1
    say(restart_line(serve_record()))
    return 0


def _stderr(line: str) -> None:
    import sys

    print(line, file=sys.stderr, flush=True)


# ── the check a running runtime makes ───────────────────────────────────────


def state_path(home: str | Path | None = None) -> Path:
    from papaya_agent_runtime.paths import ppy_home

    return Path(home) / STATE_FILE if home is not None else ppy_home() / STATE_FILE


def read_state(home: str | Path | None = None) -> dict[str, Any]:
    try:
        data = json.loads(state_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(changes: Mapping[str, Any], home: str | Path | None = None) -> None:
    path = state_path(home)
    data = {**read_state(home), **changes}
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _parse(stamp: object) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Notice:
    """An update the owner has not been told about: upstream's head and how far behind."""

    upstream_sha: str
    behind: int

    @property
    def text(self) -> str:
        return (
            f"A runtime update is available ({_changes(self.behind)}). Run {COMMAND}, then restart."
        )

    @property
    def dedupe_key(self) -> str:
        return NOTICE_KEY_PREFIX + self.upstream_sha

    def owner_message(self) -> Any:
        from papaya_agent_runtime import outreach, papaya_events

        return outreach.OwnerMessage(
            body=self.text, kind=papaya_events.OWNER_DM_NOTICE, dedupe_key=self.dedupe_key
        )

    @property
    def summary(self) -> str:
        return f"told the owner a runtime update is available ({_changes(self.behind)})"


def check_due(
    now: datetime,
    *,
    root: str | None = None,
    git: GitRunner | None = None,
    home: str | Path | None = None,
) -> Notice | None:
    """Fetch when the last check is :data:`CHECK_EVERY_SECONDS` old; the notice owed, if any.

    One fetch with a bounded timeout. A failed fetch is one log line and no notice;
    the next check is still six hours on, so an offline machine logs four lines a
    day. A notice is owed when this checkout is behind and the owner has not yet
    been told about this upstream head (:func:`record_notified`). The clock is kept
    in ``update.json``, so serve and a session's heartbeat share it and a restart
    does not fetch again.
    """
    state = read_state(home)
    last = _parse(state.get("checked_at")) if state.get("checked_at") else None
    if last is not None and (now - last).total_seconds() < CHECK_EVERY_SECONDS:
        return None
    root = root or checkout_root()
    up = upstream(root, git)
    if up is None or current_branch(root, git) != up.branch:
        return None  # nothing this checkout could be updated from
    _write_state({"checked_at": now.isoformat()}, home)
    failed = fetch(root, up, git)
    if failed:
        log.warning("[update] Could not check %s for a runtime update: %s", up.name, failed)
        return None
    found = position(root, up, git)
    if found is None or found.behind == 0:
        return None
    if state.get("notified_sha") == found.upstream:
        return None
    return Notice(upstream_sha=found.upstream, behind=found.behind)


def record_notified(upstream_sha: str, home: str | Path | None = None) -> None:
    """The owner was told about ``upstream_sha``: not again until upstream moves."""
    _write_state({"notified_sha": upstream_sha}, home)


def step(
    now: datetime,
    *,
    say: Callable[..., bool],
    root: str | None = None,
    git: GitRunner | None = None,
    home: str | Path | None = None,
) -> list[str]:
    """The check and its notice, as a session's heartbeat runs it. Never raises.

    ``say(text, owner=OwnerMessage)`` is `outreach.post_dm`: the owner's DM channel
    with this agent, else Papaya's owner-DM route. Recorded only when it landed.
    """
    try:
        notice = check_due(now, root=root, git=git, home=home)
        if notice is None:
            return []
        if not say(notice.text, owner=notice.owner_message()):
            return []
        record_notified(notice.upstream_sha, home)
        return [notice.summary]
    except Exception as exc:  # noqa: BLE001 - the heartbeat keeps ticking
        log.warning("[update] Could not check for a runtime update: %s", exc)
        return []
