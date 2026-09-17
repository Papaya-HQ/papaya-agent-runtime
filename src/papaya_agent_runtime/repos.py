"""Repository registration (`ppy repo add|list|sync`).

Registers repositories as read-only base clones under ``.ppy/repos/``. Every base
clone is cloned from its forge, and its ``origin`` is the forge. A local path is
only evidence: it names the forge (through its own ``origin``) and seeds memory,
and is never fetched from. Task worktrees (M2) branch from these bases via the
worktree lease manager.

The base branch is the forge's too (2026-09-16): a backend repository registered
from a person's checkout recorded the branch that checkout happened to be on, its
base clone's ``origin`` was that checkout, and every backend worker for days
branched from their feature branch. Registration and sync now read the forge's ``HEAD``
(``git ls-remote --symref``), sync rewrites a local-path ``origin`` to the forge, and
only ``ppy repo set --default-branch`` overrides the forge, as a lock.

``sync`` is the command that keeps a base clone honest. It used to fetch and then
record the base clone's *local* ``HEAD``, which never moved — so a base clone that
had been registered weeks earlier kept reporting a stale commit and every worker
dispatched without an explicit starting branch began from it (2026-08-31: a
backend base clone sat on a local `main` far behind the remote). Sync now
fast-forwards the base clone's default branch to the remote tip and records
*that* commit, and it refuses to touch a base clone somebody has been editing by
hand.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from papaya_agent_runtime.memory import seed_repo_memory
from papaya_agent_runtime.paths import ensure_layout, repos_dir
from papaya_agent_runtime.state import init_db, store

log = logging.getLogger("papaya_agent_runtime.repos")

#: How long one question to the forge (`ls-remote`, a fetch for `set-head`) may take.
FORGE_TIMEOUT_SECONDS = 60.0

# A base clone is not a workspace, but `ppy`'s own state directory can end up
# inside one: ``ppy_home()`` falls back to ``<cwd>/.ppy`` when ``PPY_HOME`` is unset,
# and ``ensure_layout()`` then creates the whole tree wherever the command ran.
# The ``bin/ppy`` launcher exports ``PPY_HOME``, so this only happens when ``ppy`` is
# invoked another way (``python -m papaya_agent_runtime``, or a ``ppy`` shim that is not
# the launcher) from inside a base clone — which is exactly what a `cd` into
# ``.ppy/repos/<name>` followed by a bare ``ppy ...`` does. The directory is empty
# state nobody reads; sync reports it and ``--clean-stray-ppy`` removes it.
STRAY_PPY_CAUSE = (
    "an `ppy` command was run with this directory as its working directory without "
    "PPY_HOME set (the bin/ppy launcher always sets it, so this is a bare `ppy` or "
    "`python -m papaya_agent_runtime` after a `cd` into the base clone); ppy then created "
    "its state tree here instead of in the instance home"
)


# Where a pull request can actually be opened. A repo registered from a local
# path has a local `origin`, which is no forge at all: on 2026-09-04 a worker on
# a path-registered repo pushed fine and `ppy deliver` had nowhere to open the
# pull request, so the manager pushed to GitHub and opened it by hand.
_FORGE_URL_PATTERNS = (
    re.compile(r"^https://(?:[^@/]+@)?github\.com/(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$"),
    re.compile(r"^git@github\.com:(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$"),
    re.compile(r"^ssh://git@github\.com/(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$"),
)

# The second remote an older base clone got when its own `origin` was a local
# checkout. Clones are now made from the forge and `sync` rewrites such an
# `origin`, so this remote only exists on a clone that has not been repaired yet
# (the forge could not be reached); worktrees share the base clone's remote
# configuration, so a worker can still push there meanwhile.
FORGE_REMOTE = "forge"


class RepoError(Exception):
    pass


def forge_slug(url: str | None) -> str | None:
    """``owner/name`` for a GitHub URL, else None. This is what ``gh --repo`` wants."""
    if not url:
        return None
    for pattern in _FORGE_URL_PATTERNS:
        match = pattern.match(url.strip())
        if match:
            return f"{match.group('owner')}/{match.group('name')}"
    return None


def is_forge_url(url: str | None) -> bool:
    """True for a GitHub URL (https or ssh) — somewhere a pull request can be opened."""
    return forge_slug(url) is not None


# A remote that is not a path on this filesystem: anything carrying a URL scheme
# (`https://`, `ssh://`, `file://`, ...) or written scp-style (`git@host:owner/x`).
_REMOTE_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_SCP_LIKE = re.compile(r"^[^/@]+@[^/:]+:")


def is_local_remote(url: str | None) -> bool:
    """True when a git remote URL is a plain path on this machine.

    The fake provider is only safe against a local remote: it writes a stub and
    pushes it, which on a real forge is a junk branch strangers can see (issue
    #49). Anything with a scheme or an scp-style ``user@host:`` prefix is off the
    machine; a remote that cannot be read at all counts as non-local, because
    "unknown" is not a reason to push.
    """
    if not url or not url.strip():
        return False
    candidate = url.strip()
    return not (_REMOTE_SCHEME.match(candidate) or _SCP_LIKE.match(candidate))


@dataclass
class AddedRepo:
    name: str
    origin: str
    local_path: str
    default_branch: str | None
    base_sha: str | None
    forge_url: str | None = None
    #: One line each for anything worth saying, e.g. a checkout on another branch.
    notes: list[str] = field(default_factory=list)


@dataclass
class SyncResult:
    """What one ``ppy repo sync`` did, in enough detail to report it plainly."""

    name: str
    default_branch: str | None
    base_sha: str
    previous_sha: str | None
    fast_forwarded: bool
    remote: str = "origin"
    already_current: bool = False
    stray_ppy: bool = False
    stray_ppy_removed: bool = False
    notes: list[str] = field(default_factory=list)
    #: The stored default branch this sync replaced with the forge's HEAD, if it did.
    default_branch_was: str | None = None
    #: The local path `origin` pointed at before this sync made it the forge, if it did.
    origin_was: str | None = None


def _git(args: list[str], cwd: str | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RepoError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _git_ok(args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def derive_name(url_or_path: str) -> str:
    base = url_or_path.rstrip("/")
    base = re.sub(r"\.git$", "", base)
    name = base.split("/")[-1]
    if ":" in name and "/" not in name:  # owner:repo edge
        name = name.split(":")[-1]
    name = re.sub(r"[^A-Za-z0-9._-]", "-", name)
    if not name:
        raise RepoError(f"could not derive a repo name from {url_or_path!r}")
    return name


def _resolve_source(url_or_path: str) -> str:
    """Expand a local path to an absolute path; leave URLs untouched."""
    if os.path.exists(os.path.expanduser(url_or_path)):
        return os.path.abspath(os.path.expanduser(url_or_path))
    return url_or_path


def remote_url(path: str, remote: str = "origin") -> str | None:
    """A git repository's configured URL for ``remote``, or None when it has none.

    The configured value, not `git remote get-url`'s: that one expands
    ``url.<base>.insteadOf``, and whether ``origin`` is a local path is a question
    about what the clone was told, not where git would end up fetching.
    """
    rc, out, _err = _git_ok(["config", "--get", f"remote.{remote}.url"], cwd=path)
    return out or None if rc == 0 else None


def _same_url(left: str | None, right: str | None) -> bool:
    return bool(left and right) and left.strip().rstrip("/") == right.strip().rstrip("/")


def _forge_git(args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    """A git command that talks to a forge: never prompts, and gives up after a minute."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=FORGE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {FORGE_TIMEOUT_SECONDS:g}s"
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def forge_head(forge_url: str | None) -> str | None:
    """The forge's default branch, from `git ls-remote --symref <forge> HEAD`; None if unread."""
    if not forge_url:
        return None
    rc, out, _err = _forge_git(["ls-remote", "--symref", forge_url, "HEAD"])
    if rc != 0:
        return None
    for line in out.splitlines():
        ref, _, target = line.partition("\t")
        if target.strip() == "HEAD" and ref.startswith("ref: refs/heads/"):
            return ref[len("ref: refs/heads/") :].strip() or None
    return None


def forge_branch_tip(url: str, branch: str, cwd: str | None = None) -> tuple[bool, str | None]:
    """``(read, sha)`` for ``branch`` on the forge, asked now with `git ls-remote`.

    ``(True, None)`` is a branch the forge does not have; ``(False, None)`` is a
    forge that could not be asked, which is never the same thing.
    """
    rc, out, _err = _forge_git(["ls-remote", url, f"refs/heads/{branch}"], cwd=cwd)
    if rc != 0:
        return False, None
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == f"refs/heads/{branch}" and sha.strip():
            return True, sha.strip()
    return True, None


def default_branch_from_forge(local_path: str, forge_url: str | None) -> str | None:
    """Which branch the forge calls its default, never which one a checkout is on.

    The forge's ``HEAD`` by ``ls-remote``; failing that, the clone's
    ``origin/HEAD`` (after asking the forge for it) when ``origin`` is the forge;
    failing that, ``main`` or ``master`` if the clone has one from ``origin``.
    """
    head = forge_head(forge_url)
    if head:
        return head
    origin = remote_url(local_path)
    if forge_url and _same_url(origin, forge_url):
        for attempt in range(2):
            rc, out, _err = _git_ok(
                ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=local_path
            )
            if rc == 0 and out:
                return out.split("/", 1)[-1]
            if attempt == 0:
                _forge_git(["remote", "set-head", "origin", "--auto"], cwd=local_path)
    for candidate in ("main", "master"):
        rc, _out, _err = _git_ok(
            ["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{candidate}"],
            cwd=local_path,
        )
        if rc == 0:
            return candidate
    return None


def current_branch(path: str) -> str | None:
    """The branch a checkout is on, or None (detached, or not a repository)."""
    rc, out, _err = _git_ok(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
    return out if rc == 0 and out and out != "HEAD" else None


def check_out(local_path: str, branch: str) -> None:
    """Put the base clone on ``branch``, creating it from ``origin/<branch>`` if it is new."""
    if current_branch(local_path) == branch:
        return
    rc, _out, _err = _git_ok(
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], local_path
    )
    if rc == 0:
        _git(["checkout", "--quiet", branch], cwd=local_path)
    else:
        _git(["checkout", "--quiet", "-b", branch, "--track", f"origin/{branch}"], cwd=local_path)


def ensure_forge_remote(local_path: str, forge_url: str | None) -> str:
    """Make the forge reachable from the base clone, and name the remote to use.

    When the clone's own ``origin`` is the forge — every clone made or repaired
    since 2026-09-16 — that is the remote. A clone whose ``origin`` has not been
    repaired yet (see :func:`repair_origin`) gets the forge as a *second* remote
    (``forge``) meanwhile. Returns the remote name to fetch and push with.
    """
    if not forge_url:
        return "origin"
    if _same_url(remote_url(local_path), forge_url):
        return "origin"
    existing = remote_url(local_path, FORGE_REMOTE)
    if existing is None:
        _git(["remote", "add", FORGE_REMOTE, forge_url], cwd=local_path)
    elif existing.rstrip("/") != forge_url.rstrip("/"):
        _git(["remote", "set-url", FORGE_REMOTE, forge_url], cwd=local_path)
    return FORGE_REMOTE


def upstream_remote(row) -> str:
    """The remote a registered repo's work actually travels through."""
    forge = row["forge_url"] if "forge_url" in row.keys() else None  # noqa: SIM118 - sqlite3.Row
    if not forge:
        return "origin"
    return ensure_forge_remote(row["local_path"], forge)


def _resolve_forge_url(source: str, forge_url: str | None) -> str:
    """Decide where this repo's pull requests will be opened, or refuse.

    An explicit ``--forge-url`` always wins. Otherwise a forge URL registered
    directly is its own forge, and a local path inherits the forge from its own
    ``origin``. A local path whose origin is another local path — or which has no
    origin at all — has no forge, and registering it silently is what left a
    delivered task with nowhere to open a pull request.
    """
    if forge_url:
        return forge_url.strip()
    if is_forge_url(source):
        return source
    origin = remote_url(source) if os.path.isdir(source) else None
    if is_forge_url(origin):
        assert origin is not None
        return origin
    detail = f"its origin is {origin!r}" if origin else "it has no origin remote"
    raise RepoError(
        f"{source} has no forge to open pull requests against ({detail}). Register it "
        "with `ppy repo add <path> --forge-url https://github.com/<owner>/<repo>` so "
        "delivery knows where the pull request goes."
    )


def add_repo(url_or_path: str, name: str | None = None, forge_url: str | None = None) -> AddedRepo:
    ensure_layout()
    conn = init_db()
    repo_name = name or derive_name(url_or_path)
    if store.get_repo(conn, repo_name) is not None:
        raise RepoError(f"repo {repo_name!r} is already registered")

    source = _resolve_source(url_or_path)
    resolved_forge = _resolve_forge_url(source, forge_url)
    dest = repos_dir() / repo_name
    if dest.exists():
        raise RepoError(f"destination {dest} already exists")

    # The clone comes from the forge, so its `origin` is the forge. A local path
    # only told us where that is; its branches and unpushed commits are not ours.
    try:
        _git(["clone", "--quiet", resolved_forge, str(dest)])
    except RepoError as exc:
        raise RepoError(
            f"could not clone {repo_name} from its forge {resolved_forge}: {exc}. A base "
            "clone is always made from the forge, never from a local checkout; check "
            "that this machine can reach it (`gh auth status`), then register again."
        ) from exc

    notes: list[str] = []
    default_branch = default_branch_from_forge(str(dest), resolved_forge) or current_branch(
        str(dest)
    )
    if default_branch:
        check_out(str(dest), default_branch)
    checkout_branch = current_branch(source) if os.path.isdir(source) else None
    if checkout_branch and default_branch and checkout_branch != default_branch:
        notes.append(
            f"{source} is checked out on {checkout_branch}; the base clone follows the "
            f"forge's default branch, {default_branch}"
        )
    try:
        base_sha = _git(["rev-parse", "HEAD"], cwd=str(dest))
    except RepoError:
        base_sha = None

    store.add_repo(
        conn,
        name=repo_name,
        origin=source,
        local_path=str(dest),
        default_branch=default_branch,
        base_sha=base_sha,
        forge_url=resolved_forge,
    )
    # Seed per-repo memory so learnings about this repo persist across sessions.
    seed_repo_memory(repo_name, origin=source, default_branch=default_branch)
    return AddedRepo(
        repo_name, source, str(dest), default_branch, base_sha, resolved_forge, notes=notes
    )


def list_repos() -> list[dict]:
    conn = init_db()
    return [dict(r) for r in store.list_repos(conn)]


# ── Where does a ticket's language actually live? ────────────────────────────

#: How many files to name per repository. The point of a hit list is to be read
#: in one glance by whoever is placing a ticket, not to be complete.
MAX_LOCATE_FILES = 8


@dataclass
class LocateHit:
    """One registered repository's answer to "does this language occur here?"."""

    repo: str
    local_path: str
    #: Matching file paths, most matches first, capped at :data:`MAX_LOCATE_FILES`.
    files: list[str] = field(default_factory=list)
    #: How many files matched in all, before the cap on `files`.
    file_count: int = 0
    matches: int = 0
    #: Why this repository could not be searched, when it could not be.
    note: str = ""

    @property
    def found(self) -> bool:
        return self.matches > 0


def locate(terms: list[str]) -> list[LocateHit]:
    """Which registered clones contain these strings, and in which files.

    A mechanical primitive and nothing more: it greps, counts and sorts. Whether
    a hit means the ticket belongs to that repository is a judgment, and it
    belongs to the manager turn that asked — this only stops the turn guessing
    from a repository name when the code could simply have been read.

    Every term must occur somewhere in the file for it to count, so two words
    from one ticket narrow the answer instead of widening it. Search is literal
    and case-insensitive: ticket prose quotes identifiers and UI strings, it does
    not write regular expressions.
    """
    wanted = [term for term in (t.strip() for t in terms) if term]
    if not wanted:
        raise RepoError("locate needs at least one term to search for")
    hits: list[LocateHit] = []
    for row in list_repos():
        name = str(row.get("name") or "")
        path = str(row.get("local_path") or "")
        hit = LocateHit(repo=name, local_path=path)
        if not path or not Path(path).is_dir():
            hit.note = f"the base clone is missing at {path or '(unrecorded)'}; `ppy repo sync`"
            hits.append(hit)
            continue
        counts = _grep_counts(path, wanted)
        hit.matches = sum(counts.values())
        hit.file_count = len(counts)
        hit.files = sorted(counts, key=lambda f: (-counts[f], f))[:MAX_LOCATE_FILES]
        hits.append(hit)
    # Most matches first, then by name, so two repositories that both hit are in
    # a stable order and the strongest candidate is the one read first.
    hits.sort(key=lambda h: (-h.matches, h.repo))
    return hits


def _grep_counts(path: str, terms: list[str]) -> dict[str, int]:
    """Per-file match counts for files containing *every* term, via `git grep`.

    Tracked files only, which is the point of searching a base clone: build
    output and vendored dependencies are not what a ticket is about. Each term is
    counted separately and the file's total is their sum; a file missing any term
    is dropped, which is how several terms narrow rather than widen.
    """
    per_term: list[dict[str, int]] = []
    for term in terms:
        rc, out, _err = _git_ok(["grep", "-I", "-i", "-c", "-F", "-e", term], cwd=path)
        # 1 is `git grep`'s "no match", which is an answer, not a failure. Any
        # other non-zero (not a repository, a broken index) leaves this term
        # empty, and the intersection below then reports the repository as cold.
        if rc not in (0, 1):
            return {}
        counts: dict[str, int] = {}
        for line in out.splitlines():
            file, _, count = line.rpartition(":")
            if file and count.isdigit():
                counts[file] = int(count)
        per_term.append(counts)
    if not per_term:
        return {}
    common = set(per_term[0])
    for counts in per_term[1:]:
        common &= set(counts)
    return {file: sum(counts[file] for counts in per_term) for file in common}


@dataclass
class ProvisionSettings:
    """What a repo wants done to a fresh worktree before its worker starts."""

    repo: str
    command: str | None
    reuse_venv: str | None

    @property
    def configured(self) -> bool:
        return bool(self.command or self.reuse_venv)

    def describe(self) -> str:
        if not self.configured:
            return f"{self.repo}: no worktree provisioning configured"
        parts = []
        if self.reuse_venv:
            parts.append(f"reuses {self.reuse_venv} from the base clone")
        if self.command:
            parts.append(f"runs `{self.command}` in each new worktree")
        return f"{self.repo}: " + ", and ".join(parts)


def _require_repo(name: str):
    conn = init_db()
    row = store.get_repo(conn, name)
    if row is None:
        raise RepoError(f"repo {name!r} is not registered")
    return conn, row


def get_provision(name: str) -> ProvisionSettings:
    _conn, row = _require_repo(name)
    keys = row.keys()
    return ProvisionSettings(
        repo=name,
        command=row["provision_command"] if "provision_command" in keys else None,
        reuse_venv=row["provision_venv"] if "provision_venv" in keys else None,
    )


@dataclass
class RepoSettings:
    """The per-repo knobs `ppy repo set` owns, and where each value came from."""

    repo: str
    migrations_glob: str
    migrations_glob_is_default: bool
    # The environment block a worker is handed at dispatch (issue #60).
    environment: object = None

    def describe(self) -> str:
        origin = "the default" if self.migrations_glob_is_default else "set for this repo"
        lines = [f"{self.repo}: migrations live at {self.migrations_glob} ({origin})"]
        if self.environment is not None:
            lines.extend(f"  {line}" for line in self.environment.describe())
        return "\n".join(lines)


def get_settings(name: str) -> RepoSettings:
    from papaya_agent_runtime import environment
    from papaya_agent_runtime.migrations import DEFAULT_MIGRATIONS_GLOB, glob_for_repo

    _conn, row = _require_repo(name)
    glob = glob_for_repo(row)
    return RepoSettings(
        repo=name,
        migrations_glob=glob,
        migrations_glob_is_default=glob == DEFAULT_MIGRATIONS_GLOB,
        environment=environment.for_repo(row),
    )


def set_settings(
    name: str, *, migrations_glob: str | None = None, **environment_fields: object
) -> RepoSettings:
    """Configure a repo's settings. An empty string puts a field back on the default.

    ``environment_fields`` are the environment-block columns (``compose_stack``,
    ``db_port_base``, ``db_port_variable``, ``push_hook_runs_full_suite``, ``local_gate``,
    ``full_suite_owner``, ``evidence_dir``, URL templates, source ceiling and localhost
    sandbox fact); a refused value raises ``RepoError``.
    """
    from papaya_agent_runtime import environment

    conn, _row = _require_repo(name)
    if migrations_glob is not None:
        store.update_repo_fields(conn, name, migrations_glob=migrations_glob.strip() or None)
    try:
        environment.set_fields(conn, name, **environment_fields)
    except environment.RepoEnvironmentError as exc:
        raise RepoError(str(exc)) from exc
    return get_settings(name)


def set_provision(
    name: str,
    *,
    command: str | None = None,
    reuse_venv: str | None = None,
    clear: bool = False,
) -> ProvisionSettings:
    """Configure (or clear) a repo's worktree provisioning.

    ``None`` leaves a field as it was; an empty string clears that one field, and
    ``clear`` clears both — so turning provisioning off never needs a DB edit.
    """
    conn, _row = _require_repo(name)
    fields: dict[str, object] = {}
    if clear:
        fields = {"provision_command": None, "provision_venv": None}
    else:
        if command is not None:
            fields["provision_command"] = command.strip() or None
        if reuse_venv is not None:
            cleaned = reuse_venv.strip().strip("/")
            if cleaned.startswith("..") or os.path.isabs(reuse_venv.strip()):
                raise RepoError(
                    f"--reuse-venv wants a path inside the repo, like backend/.venv; "
                    f"got {reuse_venv!r}"
                )
            fields["provision_venv"] = cleaned or None
    store.update_repo_fields(conn, name, **fields)
    return get_provision(name)


# --------------------------------------------------------------------------- #
# Sync
# --------------------------------------------------------------------------- #


def dirty_paths(local_path: str) -> list[str]:
    """Uncommitted or untracked paths in a base clone, ignoring a stray ``.ppy/``.

    A base clone is meant to be read-only: worktrees branch from it, nobody edits
    it. Anything here is either a hand edit that a fast-forward would clobber, or
    build output nobody meant to keep — both are reasons to stop and say so. The
    one exception is ``.ppy/``, which is machine state accidentally created inside
    the clone (see ``STRAY_PPY_CAUSE``) and is reported separately.
    """
    proc = subprocess.run(
        ["git", "-C", local_path, "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RepoError(f"{local_path} is not a readable git clone: {proc.stderr.strip()}")
    paths: list[str] = []
    for line in proc.stdout.splitlines():
        # Porcelain v1 lines are `XY <path>`; a rename is `XY <old> -> <new>`.
        path = line[2:].strip().split(" -> ")[-1].strip('"')
        if not path or path == ".ppy" or path.startswith(".ppy/"):
            continue
        paths.append(path)
    return sorted(set(paths))


def stray_ppy_dir(local_path: str) -> Path | None:
    """The stray ``.ppy`` directory inside a base clone, if one is there."""
    candidate = Path(local_path) / ".ppy"
    return candidate if candidate.is_dir() else None


def _cell(row, key: str):
    return row[key] if key in row.keys() else None  # noqa: SIM118 - sqlite3.Row


def is_default_branch_locked(row) -> bool:
    return bool(_cell(row, "default_branch_locked"))


def _default_branch(row, remote: str) -> str:
    """The branch sync follows: a locked one as stored, else the forge's, else the record."""
    recorded = row["default_branch"]
    if recorded and is_default_branch_locked(row):
        return str(recorded)
    forge = _cell(row, "forge_url")
    if remote == "origin":
        found = default_branch_from_forge(row["local_path"], forge)
    else:
        found = forge_head(forge)
    if found:
        return found
    if recorded:
        return str(recorded)
    raise RepoError(
        f"repo {row['name']!r} has no default branch on record and the forge's could not "
        f"be read; `ppy repo set {row['name']} --default-branch <branch>`"
    )


def repair_origin(name: str) -> str | None:
    """Make a base clone's ``origin`` its forge when it points at a local path.

    Returns the local path it replaced, or None when there was nothing to repair.
    Raises when ``origin`` is a local path and the forge cannot be reached: fetching
    from the checkout instead is how a feature branch became a base branch
    (2026-09-16), so that is refused, and readiness says `repo_origin_is_local`.
    """
    conn = init_db()
    row = store.get_repo(conn, name)
    if row is None:
        raise RepoError(f"repo {name!r} is not registered")
    forge = _cell(row, "forge_url")
    local_path = row["local_path"]
    origin = remote_url(local_path)
    if not forge or _same_url(origin, forge) or not is_local_remote(origin):
        return None
    rc, _out, err = _forge_git(["ls-remote", "--symref", forge, "HEAD"])
    if rc != 0:
        raise RepoError(
            f"{name}'s base clone fetches from the local path {origin}, not its forge "
            f"{forge}, and the forge cannot be reached to repair it ({err or 'no answer'})"
        )
    _git(["remote", "set-url", "origin", forge], cwd=local_path)
    log.info(
        "[repos] %s: origin was the local path %s; it is now the forge %s", name, origin, forge
    )
    return origin


def set_default_branch(name: str, branch: str) -> str | None:
    """Pin ``name``'s default branch over the forge's, or unpin it with an empty branch.

    Returns the branch now on record (for an unpin, the forge's when it can be read).
    """
    conn, row = _require_repo(name)
    branch = branch.strip()
    if branch:
        store.update_repo_fields(conn, name, default_branch=branch, default_branch_locked=1)
        return branch
    found = default_branch_from_forge(row["local_path"], _cell(row, "forge_url"))
    fields: dict[str, object] = {"default_branch_locked": None}
    if found:
        fields["default_branch"] = found
    store.update_repo_fields(conn, name, **fields)
    return found or row["default_branch"]


def _fast_forward(local_path: str, branch: str, remote: str) -> tuple[str, bool]:
    """Move the base clone's ``branch`` to ``<remote>/<branch>``. Never rewrites.

    Returns the resulting commit and whether the branch actually moved. A branch
    that has drifted away from the remote (someone committed in the base clone)
    cannot fast-forward: that is refused rather than reset, because a reset here
    would silently destroy the only copy of those commits.
    """
    target_rc, target, _err = _git_ok(["rev-parse", f"{remote}/{branch}"], cwd=local_path)
    if target_rc != 0 or not target:
        raise RepoError(
            f"{remote}/{branch} does not exist in the base clone after fetching; "
            f"is {branch!r} still the default branch?"
        )
    current_rc, current_branch, _err = _git_ok(
        ["rev-parse", "--abbrev-ref", "HEAD"], cwd=local_path
    )
    before_rc, before, _err = _git_ok(["rev-parse", branch], cwd=local_path)
    before = before if before_rc == 0 else ""

    if before == target:
        return target, False

    if current_rc == 0 and current_branch == branch:
        rc, _out, err = _git_ok(["merge", "--ff-only", "--quiet", f"{remote}/{branch}"], local_path)
    else:
        # Not checked out here: update the ref directly. `git fetch <remote>
        # <branch>:<branch>` is fast-forward-only for a branch that is not HEAD,
        # so a diverged base clone fails instead of being rewritten.
        rc, _out, err = _git_ok(["fetch", "--quiet", remote, f"{branch}:{branch}"], local_path)
    if rc != 0:
        raise RepoError(
            f"the base clone's {branch!r} cannot fast-forward to {remote}/{branch} "
            f"({(before or '?')[:8]} vs {target[:8]}) — it holds commits the remote does "
            f"not: {err or 'non-fast-forward'}"
        )
    after = _git(["rev-parse", branch], cwd=local_path)
    return after, after != before


def keep_base_clones_right() -> list[str]:
    """`ppy serve`'s start remedy: every base clone back on its forge. One line per repair.

    A clone needs one when its ``origin`` is a local path, when its stored default
    branch is not the forge's HEAD (and was not pinned), or when it is checked out
    on some other branch. The repair is :func:`sync_repo`, which says what it
    changed; a clone that cannot be repaired (dirty, diverged, forge unreachable)
    gets one line saying why and is left as it was. A clone with nothing wrong costs
    one `ls-remote` and says nothing.
    """
    lines: list[str] = []
    for row in list_repos():
        name, local_path, forge = row["name"], row.get("local_path"), row.get("forge_url")
        if not forge or not local_path or not Path(local_path).is_dir():
            continue
        origin = remote_url(local_path)
        stored = row.get("default_branch")
        wrong_origin = is_local_remote(origin) and not _same_url(origin, forge)
        head = None if row.get("default_branch_locked") or wrong_origin else forge_head(forge)
        wrong_branch = bool(head and head != stored)
        want = head or stored
        off_branch = bool(want and current_branch(local_path) not in (None, want))
        if not (wrong_origin or wrong_branch or off_branch):
            continue
        try:
            result = sync_repo(name)
        except RepoError as exc:
            lines.append(f"could not put {name}'s base clone back on its forge: {exc}")
            continue
        said = "; ".join(result.notes) or f"checked out {result.default_branch}"
        lines.append(f"repaired {name}'s base clone: {said}")
    return lines


def sync_repo(name: str, *, clean_stray_ppy: bool = False) -> SyncResult:
    """Fetch, fast-forward the base clone's default branch, and record that commit.

    Refuses (changing nothing) when the base clone has uncommitted changes or
    untracked files outside ``.ppy/`` — a fast-forward over a hand edit is how work
    disappears — and reports a stray ``.ppy/`` directory so it can be cleaned.
    """
    conn = init_db()
    row = store.get_repo(conn, name)
    if row is None:
        raise RepoError(f"repo {name!r} is not registered")
    local_path = row["local_path"]
    previous = row["base_sha"]

    dirty = dirty_paths(local_path)
    if dirty:
        shown = ", ".join(dirty[:10])
        more = f" (+{len(dirty) - 10} more)" if len(dirty) > 10 else ""
        raise RepoError(
            f"refusing to sync {name!r}: the base clone has uncommitted changes or "
            f"untracked files — {shown}{more}. A base clone is read-only; move or "
            "remove these, then sync again."
        )

    origin_was = repair_origin(name)
    row = store.get_repo(conn, name)
    remote = upstream_remote(row)
    _git(["fetch", "--quiet", "--prune", remote], cwd=local_path)
    branch = _default_branch(row, remote)
    base_sha, moved = _fast_forward(local_path, branch, remote)
    if remote == "origin":
        check_out(local_path, branch)
    store.update_repo_base_sha(conn, name, base_sha)
    recorded = row["default_branch"]
    if recorded != branch:
        store.update_repo_fields(conn, name, default_branch=branch)

    result = SyncResult(
        name=name,
        default_branch=branch,
        base_sha=base_sha,
        previous_sha=previous,
        fast_forwarded=moved,
        remote=remote,
        already_current=not moved,
        origin_was=origin_was,
        default_branch_was=recorded if recorded and recorded != branch else None,
    )
    if origin_was:
        result.notes.append(f"origin was the local path {origin_was}; it is now the forge")
    if result.default_branch_was:
        line = (
            f"default branch was {result.default_branch_was}; it is now {branch}, the forge's HEAD"
        )
        log.info("[repos] %s: %s", name, line)
        result.notes.append(line)

    stray = stray_ppy_dir(local_path)
    if stray is not None:
        result.stray_ppy = True
        if clean_stray_ppy:
            shutil.rmtree(stray, ignore_errors=True)
            result.stray_ppy_removed = not stray.exists()
            result.notes.append(f"removed the stray {stray} directory")
        else:
            result.notes.append(
                f"a stray {stray} directory is sitting inside the base clone: "
                f"{STRAY_PPY_CAUSE}. Remove it with `ppy repo sync {name} --clean-stray-ppy`."
            )
    return result
