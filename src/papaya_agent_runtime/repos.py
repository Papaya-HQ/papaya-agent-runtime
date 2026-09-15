"""Repository registration (`ppy repo add|list|sync`).

Registers repositories as read-only base clones under ``.ppy/repos/``. A remote
URL is cloned; an existing local path is cloned locally so the base clone stays
isolated from the user's working checkout. Task worktrees (M2) branch from these
bases via the worktree lease manager.

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

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from papaya_agent_runtime.memory import seed_repo_memory
from papaya_agent_runtime.paths import ensure_layout, repos_dir
from papaya_agent_runtime.state import init_db, store

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

# The second remote a base clone gets when its own `origin` is not the forge.
# Rewriting `origin` would break the clone's relationship with the local checkout
# it was cloned from (and every worktree already leased off it), so the forge is
# added alongside it under a name nothing else uses. Worktrees share the base
# clone's remote configuration, so a worker's worktree can fetch and push there
# without any extra setup.
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
    """A git repository's URL for ``remote``, or None when it has none."""
    rc, out, _err = _git_ok(["remote", "get-url", remote], cwd=path)
    return out or None if rc == 0 else None


def ensure_forge_remote(local_path: str, forge_url: str | None) -> str:
    """Make the forge reachable from the base clone, and name the remote to use.

    When the clone's own ``origin`` is already the forge, that is the remote —
    nothing to add. Otherwise the forge is added as a *second* remote
    (``forge``), because rewriting ``origin`` would cut the clone off from the
    local checkout it was cloned from. Returns the remote name to fetch and push
    with.
    """
    if not forge_url:
        return "origin"
    origin = remote_url(local_path)
    if origin and origin.rstrip("/") == forge_url.rstrip("/"):
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

    _git(["clone", "--quiet", source, str(dest)])
    ensure_forge_remote(str(dest), resolved_forge)

    default_branch: str | None
    try:
        default_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=str(dest))
    except RepoError:
        default_branch = None
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
    return AddedRepo(repo_name, source, str(dest), default_branch, base_sha, resolved_forge)


def list_repos() -> list[dict]:
    conn = init_db()
    return [dict(r) for r in store.list_repos(conn)]


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


def _default_branch(row, remote: str) -> str:
    recorded = row["default_branch"]
    if recorded:
        return str(recorded)
    rc, out, _err = _git_ok(
        ["symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD"], cwd=row["local_path"]
    )
    if rc == 0 and out:
        return out.split("/", 1)[-1]
    rc, out, _err = _git_ok(["rev-parse", "--abbrev-ref", "HEAD"], cwd=row["local_path"])
    if rc == 0 and out and out != "HEAD":
        return out
    raise RepoError(
        f"repo {row['name']!r} has no default branch on record and none could be read "
        "from the clone; re-register it or set one"
    )


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

    remote = upstream_remote(row)
    _git(["fetch", "--quiet", "--prune", remote], cwd=local_path)
    branch = _default_branch(row, remote)
    base_sha, moved = _fast_forward(local_path, branch, remote)
    store.update_repo_base_sha(conn, name, base_sha)
    if row["default_branch"] != branch:
        store.update_repo_fields(conn, name, default_branch=branch)

    result = SyncResult(
        name=name,
        default_branch=branch,
        base_sha=base_sha,
        previous_sha=previous,
        fast_forwarded=moved,
        remote=remote,
        already_current=not moved,
    )

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
