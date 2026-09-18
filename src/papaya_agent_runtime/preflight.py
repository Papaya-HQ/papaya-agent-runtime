"""Dispatch preflight: the checks a manager kept in its head, moved into the tool.

Two dispatches were lost on 2026-08-30/31 to conditions ``ppy`` could have caught
before creating a task: a brief read from a file that no longer existed (the
worker started with empty instructions) and a worktree checkout that died on a
full disk. ``ppy dispatch --brief <file>`` now refuses an empty brief, refuses to
dispatch onto a nearly full disk, and archives the brief it sent next to the
instance state so the exact packet a worker received is always recoverable.

Since runtime #94 it also runs the trust checks (remote, base, lease, gate): the
starting point and the gate a worker is handed are checked, not assumed. The
sequencing checks (overlap, empty-parent) are refusals from the same set.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# One allowlist matcher for the gate check and the brief's allowlist check.
from papaya_agent_runtime.brief_lint import _allowed_prefixes, _covered
from papaya_agent_runtime.paths import ppy_home

MIN_FREE_GB_ENV = "PPY_MIN_FREE_GB"
DEFAULT_MIN_FREE_GB = 5.0

#: A brief's first Markdown heading, however many "#" marks it carries.
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*(.+?)\s*$")
#: A task title is a one-line label, not a paragraph; longer headings are cut here.
TITLE_CAP = 120


class PreflightError(RuntimeError):
    """A dispatch was refused before any task state was created.

    ``check`` names the overridable check that refused (one of :data:`CHECKS`),
    or is None for a refusal no flag can wave through (an empty brief, a full disk).
    """

    def __init__(self, message: str, *, check: str | None = None) -> None:
        super().__init__(message)
        self.check = check


def read_brief(path: str | Path) -> str:
    """Return the brief's text, refusing a missing or empty file plainly."""
    brief_path = Path(path).expanduser()
    if not brief_path.is_file():
        raise PreflightError(
            f"brief file not found: {brief_path} — nothing was dispatched. "
            "Write the brief to a durable location (for example .ppy/briefs/<repo>/) first."
        )
    text = brief_path.read_text(encoding="utf-8")
    if not text.strip():
        raise PreflightError(
            f"brief file is empty: {brief_path} — refusing to dispatch a worker with no "
            "instructions (this is how an empty-brief worker was sent out on 2026-08-30)."
        )
    return text


def title_from_brief(text: str) -> str | None:
    """The brief's first Markdown heading, shaped into a task title.

    A brief already opens with the outcome it wants, written carefully; a title
    typed at the prompt is the same sentence typed again, worse. Leading ``#``
    marks and surrounding whitespace go, internal whitespace collapses to single
    spaces, and the result is capped so a title stays a label. Returns ``None``
    when the brief has no heading at all, which the caller reports plainly.
    """
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match is None:
            continue
        heading = " ".join(match.group(1).split())
        if heading:
            return heading[:TITLE_CAP]
    return None


def min_free_gb() -> float:
    """The free-space floor a dispatch requires, in gigabytes (env-overridable)."""
    raw = os.environ.get(MIN_FREE_GB_ENV)
    if not raw:
        return DEFAULT_MIN_FREE_GB
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_MIN_FREE_GB


def check_disk(path: str | Path | None = None, *, floor_gb: float | None = None) -> float:
    """Return free gigabytes at ``path`` (the instance root by default) or refuse.

    A worker needs room for a worktree checkout and a dependency install — several
    gigabytes on a real repository. Dispatching below the floor produces a
    half-created checkout that also blocks the pool until someone removes it.
    """
    target = Path(path) if path is not None else ppy_home()
    probe = target if target.exists() else target.parent
    usage = shutil.disk_usage(probe)
    free_gb = usage.free / 1e9
    floor = min_free_gb() if floor_gb is None else floor_gb
    if free_gb < floor:
        raise PreflightError(
            f"only {free_gb:.1f} GB free on the volume holding {probe} (floor {floor:g} GB; "
            f"set {MIN_FREE_GB_ENV} to change it) — free space before dispatching; a "
            f"worktree checkout plus a dependency install needs several GB. {reclaim_hint()}"
        )
    return free_gb


def reclaim_hint() -> str:
    """Point a refused dispatch at the space it already owns.

    Most of the time the disk is full of the harness's own finished worktrees —
    20 GB of delivered tasks on 2026-09-02 — so a refusal that only says "free
    space" sends the manager hunting when one command would do it.
    """
    from papaya_agent_runtime.worktree.reclaim import human_bytes, reclaimable_bytes

    freeable = reclaimable_bytes()
    if freeable <= 0:
        return (
            "`ppy worktree prune` has nothing to reclaim right now; "
            "`ppy worktree list` shows what each slot is holding."
        )
    return (
        f"`ppy worktree prune` would reclaim {human_bytes(freeable)} from the worktrees of "
        "finished tasks (`ppy worktree list` shows what each slot is holding)."
    )


def archived_brief_path(repo: str, task_id: int) -> Path:
    """Where a task's brief is kept, whether or not it has been written yet."""
    return ppy_home() / "briefs" / repo / f"task-{task_id}.md"


def archive_brief(repo: str, task_id: int, text: str) -> Path:
    """Keep the exact brief a task received under the instance's briefs directory."""
    target = archived_brief_path(repo, task_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


# --------------------------------------------------------------------------- #
# Trust checks: the remote, base and gate a worker is about to be handed.
#
# In Middle Manager about 40% of worker reflections named a gate or environment
# false start (runtime #94): a gate command the worker's allowlist denied, a
# `make` target absent at base, a lease cut at the wrong commit, and a stale
# local origin that made a correct starting SHA look missing (task 229, on this
# repository). The unattended `ppy serve` manager cannot notice these by reading
# output, so dispatch refuses them itself.
# --------------------------------------------------------------------------- #

#: The checks ``ppy dispatch --accept-preflight`` can name. There is no blanket skip.
#: ``remote``, ``base`` and ``gate`` run in ``ppy dispatch`` itself; ``overlap`` and
#: ``empty-parent`` run in the supervisor before the task row exists, and ``lease``
#: once the lease does. A new check is one more name here.
CHECKS = ("remote", "base", "lease", "gate", "overlap", "empty-parent")

#: The starting-commit line the brief templates use: "you must see `cb3a761`".
_STARTING_SHA = re.compile(r"you must see\s+`?([0-9a-f]{7,40})\b`?", re.IGNORECASE)
#: A gate segment that opens with ``NAME=value``: an inline environment assignment.
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
#: A Makefile rule line: one or more targets, then ``:`` or ``::`` (never ``:=``).
_MAKE_RULE = re.compile(r"^([^\s#:=][^:=]*?)\s*::?(?!=)")
#: ``make`` options whose value is the next word.
_MAKE_VALUE_FLAGS = {"-C", "--directory", "-f", "--file", "--makefile", "-I", "-o", "-W"}


@dataclass
class DispatchTrust:
    """What the trust checks settled, carried to the supervisor for the lease check."""

    remote: str | None = None
    expect_base: str | None = None
    starting_sha: str | None = None
    #: check name -> the refusal it waved through (None when it would have passed).
    overridden: dict[str, str | None] = field(default_factory=dict)

    def accepted(self) -> list[dict]:
        return [{"check": check, "overridden": text} for check, text in self.overridden.items()]


@dataclass
class LeaseRefusal:
    message: str
    expected: str
    actual: str


def _git(cwd: str | Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False
    )


def _commit(cwd: str | Path, ref: str) -> str | None:
    proc = _git(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return (proc.stdout.strip() or None) if proc.returncode == 0 else None


def _is_ancestor(cwd: str | Path, older: str, newer: str) -> bool:
    return _git(cwd, "merge-base", "--is-ancestor", older, newer).returncode == 0


def _same_repository(a: str, b: str) -> bool:
    """Whether two remote URLs name one repository (ssh and https GitHub forms agree)."""
    from papaya_agent_runtime.repos import forge_slug

    slug_a, slug_b = forge_slug(a), forge_slug(b)
    if slug_a and slug_b:
        return slug_a.lower() == slug_b.lower()

    def bare(url: str) -> str:
        return re.sub(r"\.git$", "", url.strip().rstrip("/"))

    return bare(a) == bare(b)


def check_remote(origin: str | None, forge_url: str | None) -> None:
    """Refuse a base clone whose ``origin`` is a forge URL for another repository.

    A local-path origin is a supported registration (the forge is then a second
    remote, and the base is resolved from it), so it is not refused here; nor is
    any other origin that is not a forge URL.
    """
    from papaya_agent_runtime.repos import is_forge_url

    if not origin or not forge_url or not is_forge_url(origin):
        return
    if _same_repository(origin, forge_url):
        return
    raise PreflightError(
        f"the base clone's origin is {origin} but the registered forge is {forge_url} — "
        "the worker would start from one repository and deliver to another. Re-register "
        "the repo with `ppy repo add <path> --forge-url <forge URL>` so the two agree.",
        check="remote",
    )


def starting_sha(text: str) -> str | None:
    """The starting commit a brief names on its "you must see `<sha>`" line, if any."""
    match = _STARTING_SHA.search(text or "")
    return match.group(1).lower() if match else None


def fetch_remote(local_path: str, remote: str) -> None:
    """The one fetch dispatch makes, so the base is judged against the remote's today."""
    fetched = _git(local_path, "fetch", "--quiet", remote)
    if fetched.returncode != 0:
        raise PreflightError(
            f"`git fetch {remote}` failed in the base clone {local_path}: "
            f"{fetched.stderr.strip()[:200]} — the starting commit cannot be checked "
            "against a remote that did not answer.",
            check="base",
        )


def intended_base(
    local_path: str,
    remote: str,
    *,
    default_branch: str,
    base_branch: str | None = None,
    lease_source: str | None = None,
) -> str | None:
    """The commit the lease must sit on: the ``--base`` branch head or the default's.

    Read from ``remote`` after the fetch — never from the base clone's own default
    branch, which a stale local origin can leave behind the forge (task 229). A
    ``--base`` branch not on the remote may still be a stack parent that has not
    pushed: its local branch, or its lease worktree, is then the head.
    """
    if not base_branch:
        return _commit(local_path, f"refs/remotes/{remote}/{default_branch}")
    head = _commit(local_path, f"refs/remotes/{remote}/{base_branch}") or _commit(
        local_path, f"refs/heads/{base_branch}"
    )
    if head is None and lease_source and Path(lease_source).is_dir():
        head = _commit(lease_source, base_branch)
    if head is None:
        raise PreflightError(
            f"the branch to start from, {base_branch!r}, is not on {remote} after "
            f"`git fetch {remote}` in the base clone, and no local branch or stack "
            "parent's worktree has it — there is nothing for the worker to start from.",
            check="base",
        )
    return head


def check_starting_commit(
    local_path: str, remote: str, sha: str, *, base_branch: str | None, expect: str | None
) -> str:
    """Refuse a named starting commit the remote does not have; return its full SHA."""
    full = _commit(local_path, sha)
    on_remote = bool(
        full
        and _git(
            local_path,
            "for-each-ref",
            "--contains",
            full,
            "--format=%(refname)",
            f"refs/remotes/{remote}/",
        ).stdout.strip()
    )
    # A stack parent that has not pushed yet carries the commit only locally.
    under_base = bool(full and base_branch and expect and _is_ancestor(local_path, full, expect))
    if not (on_remote or under_base):
        raise PreflightError(
            f"the brief's starting commit {sha} is not reachable from {remote} after "
            f"`git fetch {remote}` in the base clone — the worker would be told to find a "
            "commit it cannot see. Check the SHA against the forge before dispatching.",
            check="base",
        )
    assert full is not None
    return full


def _make_invocation(words: list[str]) -> tuple[list[str], list[str]]:
    """(candidate Makefile paths from the repo root, targets) for one ``make`` call."""
    directory, makefile, targets = "", "", []
    rest = iter(words[1:])
    for word in rest:
        if word in _MAKE_VALUE_FLAGS:
            value = next(rest, "")
            if word in ("-C", "--directory"):
                directory = value
            elif word in ("-f", "--file", "--makefile"):
                makefile = value
            continue
        if word.startswith("-") or "=" in word:
            continue
        targets.append(word)
    names = [makefile] if makefile else ["GNUmakefile", "makefile", "Makefile"]
    return [str(Path(directory) / name) if directory else name for name in names], targets


def _makefile_targets(
    local_path: str, ref: str, names: list[str]
) -> tuple[str, set[str], str | None]:
    """(path read, targets, default target) parsed from the Makefile text at ``ref``.

    Parsed, not executed: ``make -n`` runs ``$(shell ...)`` and recursive
    ``$(MAKE)`` lines. The price is that included makefiles are not read.
    """
    for name in names:
        shown = _git(local_path, "show", f"{ref}:{name}")
        if shown.returncode != 0:
            continue
        targets: set[str] = set()
        default = None
        for line in shown.stdout.splitlines():
            match = _MAKE_RULE.match(line)
            if match is None:
                continue
            for target in match.group(1).split():
                if "$" in target or "%" in target:
                    continue
                targets.add(target)
                if default is None and not target.startswith("."):
                    default = target
        return name, targets, default
    return names[-1], set(), None


def check_gate(gate: str, allowed: list[str], *, local_path: str, ref: str) -> None:
    """Refuse a recorded local gate a Claude worker could not run at ``ref``.

    Each ``&&``/``;`` segment is judged on its own against the worker allowlist,
    which matches one plain command; a ``make`` segment's targets must be rules in
    the Makefile at base.
    """
    everything, prefixes = _allowed_prefixes(allowed)
    for segment in re.split(r"&&|;", gate):
        words = segment.split()
        if not words:
            continue
        command, head = " ".join(words), words[0]
        if _ASSIGNMENT.match(head):
            raise PreflightError(
                f"the repo's local gate segment `{command}` opens with the inline assignment "
                f"`{head}`, which a Claude worker's allowlist denies (it matches one plain "
                "command). Drop the assignment from the gate with `ppy repo set <repo> "
                "--local-gate ...`.",
                check="gate",
            )
        if not everything and not _covered(command, prefixes):
            shown = ", ".join(f"`{p}`" for p in prefixes) or "none"
            raise PreflightError(
                f"the repo's local gate segment `{command}` is not on the Claude worker "
                f"allowlist: it needs `Bash({head}:*)` (allowed: {shown}). The worker "
                "could not run its own gate.",
                check="gate",
            )
        if head.rsplit("/", 1)[-1] != "make":
            continue
        names, wanted = _make_invocation(words)
        path, targets, default = _makefile_targets(local_path, ref, names)
        for target in wanted or ([default] if default else ["(default target)"]):
            if target not in targets:
                raise PreflightError(
                    f"the repo's local gate segment `{command}` needs the make target "
                    f"`{target}`, but {path} at {ref[:12]} has no such rule (targets are "
                    "parsed from the Makefile text, not run).",
                    check="gate",
                )


def lease_refusal(
    worktree: str, *, expect_base: str | None, starting_sha: str | None
) -> LeaseRefusal | None:
    """Why a fresh lease is not on the intended base, or None when it is.

    A branch or default-branch head must match exactly; a brief's named starting
    commit must be the lease HEAD or one of its ancestors.
    """
    head = _commit(worktree, "HEAD") or ""
    if expect_base and head != expect_base:
        return LeaseRefusal(
            f"the lease is at {head[:12] or 'no commit'} but the intended base is "
            f"{expect_base[:12]} — run `ppy repo sync <repo>` so the base clone matches its "
            "forge, then dispatch again.",
            expect_base,
            head,
        )
    if starting_sha and not (head and _is_ancestor(worktree, starting_sha, head)):
        return LeaseRefusal(
            f"the lease is at {head[:12] or 'no commit'}, which does not contain the brief's "
            f"starting commit {starting_sha[:12]} — run `ppy repo sync <repo>`, then dispatch "
            "again.",
            starting_sha,
            head,
        )
    return None


def trust_checks(
    repo_row,
    instructions: str,
    *,
    provider: str,
    accepted: list[str],
    base_branch: str | None = None,
    lease_source: str | None = None,
    allowed: list[str] | None = None,
) -> DispatchTrust:
    """Run the remote, base and gate checks for one dispatch, before any task exists.

    A check named in ``accepted`` still runs; its refusal is recorded as what the
    override waved through instead of refusing. Any other refusal raises.
    """
    from papaya_agent_runtime import repos

    trust = DispatchTrust()

    def run(check: str, fn):
        try:
            return fn()
        except PreflightError as exc:
            if exc.check != check or check not in accepted:
                raise
            earlier = trust.overridden.get(check)
            trust.overridden[check] = f"{earlier} {exc}" if earlier else str(exc)
            return None

    columns = repo_row.keys()  # sqlite3.Row: `in row` would search values

    def cell(name: str) -> str:
        return (repo_row[name] if name in columns else None) or ""

    local = repo_row["local_path"]
    run("remote", lambda: check_remote(repos.remote_url(local), cell("forge_url")))
    remote = repos.upstream_remote(repo_row)
    trust.remote = remote
    default = cell("default_branch") or "main"
    run("base", lambda: fetch_remote(local, remote))
    trust.expect_base = run(
        "base",
        lambda: intended_base(
            local,
            remote,
            default_branch=default,
            base_branch=base_branch,
            lease_source=lease_source,
        ),
    )
    sha = starting_sha(instructions)
    if sha:
        trust.starting_sha = run(
            "base",
            lambda: check_starting_commit(
                local, remote, sha, base_branch=base_branch, expect=trust.expect_base
            ),
        )
    gate = cell("local_gate")
    if provider == "claude" and gate.strip():
        run(
            "gate",
            lambda: check_gate(
                gate, allowed or [], local_path=local, ref=trust.expect_base or "HEAD"
            ),
        )
    for check in accepted:
        trust.overridden.setdefault(check, None)
    return trust


# --------------------------------------------------------------------------- #
# Sequencing: a stack parent must have something to stack on (runtime #94).
#
# In Middle Manager a child was stacked on a parent that had not committed yet.
# The parent's branch was still its base, so the child started from `main`,
# recorded `main` as its base, and was a sibling in all but name.
# --------------------------------------------------------------------------- #


def _forge_head(local_path: str, remote: str, branch: str) -> str | None:
    """The branch's head on ``remote``, with its objects fetched when they are missing."""
    listed = _git(local_path, "ls-remote", "--quiet", remote, f"refs/heads/{branch}")
    words = listed.stdout.split() if listed.returncode == 0 else []
    if not words:
        return None
    sha = words[0]
    if _commit(local_path, sha) is None:
        _git(local_path, "fetch", "--quiet", remote, branch)
    return sha


def _commits_beyond(cwd: str, base: str, head: str) -> int | None:
    """How many commits ``head`` has that ``base`` does not; None when git cannot say."""
    if head == base:
        return 0
    proc = _git(cwd, "rev-list", "--count", f"{base}..{head}")
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


def empty_parent_refusal(parent, *, local_path: str, remote: str) -> str | None:
    """Why ``parent`` has nothing to stack on yet, or None when it has (or cannot be judged).

    The parent's branch is looked for where a child's start would find it: on the
    forge, as the base clone's local branch, and in the parent's lease worktree.
    It is empty only when it was found somewhere and every copy found has no
    commits beyond the parent's own base, so a lease-only commit that was never
    pushed counts as progress. Uncommitted edits do not: a child starts from commits.
    """
    base, branch = parent["base_sha"], parent["branch"]
    if not base or not branch:
        return None
    heads: list[tuple[str, str, str]] = []
    forge = _forge_head(local_path, remote, branch)
    if forge:
        heads.append((f"on {remote}", local_path, forge))
    local = _commit(local_path, f"refs/heads/{branch}")
    if local:
        heads.append(("in the base clone", local_path, local))
    worktree = parent["worktree_path"]
    if worktree and Path(worktree).is_dir():
        lease_head = _commit(worktree, "HEAD")
        if lease_head:
            heads.append(("in its lease worktree", worktree, lease_head))
    if not heads:
        return None
    for _where, cwd, head in heads:
        if _commits_beyond(cwd, base, head) != 0:
            return None
    where = ", ".join(dict.fromkeys(where for where, _cwd, _head in heads))
    return (
        f'the stack parent, task {parent["id"]} "{parent["title"]}" (branch {branch}), has no '
        f"commits beyond its own base {base[:12]} ({where}) — the new worker would start "
        "from that base, not from the parent's work. Wait for the parent's first push, then "
        "dispatch again; to start from its base anyway, --accept-preflight empty-parent "
        "--reason ..."
    )
