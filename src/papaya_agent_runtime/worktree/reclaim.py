"""Worktree inventory and reclamation (``ppy worktree list|prune``).

`ppy dispatch` refuses below a 5 GB free-space floor, but nothing ever gave the
slots back: on 2026-09-02 twenty gigabytes sat in the worktrees of tasks that had
already been delivered (16 backend slots, 13 frontend), and the only way to see
or reclaim them was `du` plus a hand-written `git worktree remove`.

Reclamation is deliberately conservative. A worktree is removed only when its
task is over **and** the checkout holds nothing that exists nowhere else:

- no uncommitted changes (``git status --porcelain`` is empty), and
- no commits missing from a remote (``git rev-list HEAD --not --remotes`` is 0).

Anything else is skipped and listed with the reason, so a full disk never costs
work that was never pushed.

The leases table is not the whole inventory. On 2026-09-03 the first real prune
gave back 7.0 GB from nine backend slots while thirteen frontend slot directories
under ``~/.treehouse/papaya-frontend-monorepo-bbcd55`` — about 10 GB, all clean,
all from delivered tasks — did not appear in ``ppy worktree list`` at all: their
leases were released, or were created by an earlier ppy instance whose database
this one never saw. So inventory also walks the pool directories themselves (the
treehouse pool roots for each registered repo, and the plain pool under
``.ppy/worktree-pools/``) and labels any slot no active lease owns as
**orphaned**. Orphans are reclaimed under exactly the same safety rules — clean,
and every commit already on a remote — because nothing about being unowned makes
losing work cheaper.

A pool root is not proof of ownership, and that distinction is load-bearing: the
same treehouse home holds this instance's pools next to the user's own for the
same repositories (``papaya-backend-monorepo-58a49b`` is ours,
``papaya-backend-monorepo-e5cc8b`` is theirs), and both match the same name glob.
So a slot is a reclamation candidate only when its checkout's base clone is one
of this instance's, under ``.ppy/repos/``. Everything else is listed as unmanaged
and is never removed, however clean and however pushed — a personal worktree is
not ours to reclaim, and being tidy is not consent.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from papaya_agent_runtime.paths import db_path, repos_dir, treehouse_home, worktree_pools_dir
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.worktree.lease import Lease, LeaseError, LeaseManager

# A task in one of these is over: nothing will run in its worktree again.
RECLAIMABLE_STATUSES = ("delivered", "closed", "cancelled")


@dataclass
class WorktreeEntry:
    """One leased slot, with everything needed to decide whether it can go."""

    lease_id: str
    path: str
    repo: str | None
    task_id: int | None
    task_status: str | None
    branch: str
    exists: bool = True
    dirty: bool = False
    unpushed_commits: int = 0
    size_bytes: int = 0
    reclaimable: bool = False
    reason: str = ""
    backend: str = "git"
    repo_path: str = ""
    orphaned: bool = False
    checkout_path: str = ""
    #: Whether this instance created the slot — i.e. its checkout belongs to one of
    #: our base clones. False means somebody else's worktree; never reclaimable.
    managed: bool = True
    #: The open pull request that keeps a delivered task's slot, when one does.
    open_pr: int | str | None = None

    @property
    def slot(self) -> str:
        """The pool slot name — the last path segment, which is what `du` shows."""
        return Path(self.path).name

    @property
    def checkout(self) -> str:
        """Where git actually lives for this slot.

        For a leased slot that is the slot itself. A treehouse pool slot is a
        numbered directory holding the checkout one level down, and the size on
        disk belongs to the numbered directory while the git questions belong to
        the checkout, so the two are tracked separately.
        """
        return self.checkout_path or self.path


#: The forge read a stale pull request record is refreshed with: `watch._lookup_pr`'s
#: ``(branch, cwd) -> entry``. A module attribute so a test can answer for the forge.
lookup_pr: Callable[[str, str | None], dict[str, Any]] | None = None

#: (database, task id) -> (epoch seconds, entry) of the last forge read this process made.
_asked: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}


@dataclass(frozen=True)
class HeldByPullRequest:
    pr: int | str | None
    reason: str


def _stamp_age(stamp: object, now: datetime) -> float | None:
    try:
        parsed = datetime.fromisoformat(str(stamp or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (now - parsed).total_seconds()


#: One round when nothing says otherwise (`rounds.DEFAULT_ROUNDS_INTERVAL`).
_ROUND_SECONDS = 300.0


def _refresh_after() -> float:
    """How old a pull request record may be before the forge is asked again: one round.

    Read the way `rounds.interval_from_env` reads it, without importing the rounds
    (and `serve` with them) into every `ppy worktree list`.
    """
    try:
        raw = os.environ.get("PPY_ROUNDS_INTERVAL", "").strip()
        if raw:
            return float(raw) or _ROUND_SECONDS
        from papaya_agent_runtime.config import load_config

        return float(load_config().health.rounds_interval) or _ROUND_SECONDS
    except Exception:  # noqa: BLE001 - a bad interval setting is not a reason to prune
        return _ROUND_SECONDS


def open_pull_request(
    task_id: int, *, now: datetime | None = None, refresh_after: float | None = None
) -> HeldByPullRequest | None:
    """Why a delivered task's slot stays because of its pull request, or ``None`` when it does not.

    On PAP-222 (2026-09-17) hygiene removed task 21's slot while PR 710 was open and
    red, and when the reconcile lane needed the checkout to fix CI there was nothing
    there. A delivered task whose pull request is open is not over, however clean and
    pushed its checkout is. The state is the PR watch record (`pr_observed`), read
    fresh from the forge when it is older than one round. A task with no pull request
    on record at all is left to the ordinary rules; one whose pull request exists but
    cannot be read now is kept — unknown is not merged.
    """
    import json

    from papaya_agent_runtime import team

    conn = init_db()
    try:
        observed = conn.execute(
            "SELECT payload, created_at FROM events WHERE task_id = ? AND kind = ? "
            "ORDER BY id DESC LIMIT 1",
            (task_id, team.PR_OBSERVED_EVENT),
        ).fetchone()
        delivered = conn.execute(
            "SELECT payload FROM events WHERE task_id = ? AND kind = 'delivered' "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        task = store.get_task(conn, task_id)
        repo = (
            conn.execute("SELECT local_path FROM repos WHERE id = ?", (task["repo_id"],)).fetchone()
            if task is not None and task["repo_id"]
            else None
        )
    finally:
        conn.close()

    def load(row: Any) -> dict[str, Any]:
        try:
            value = json.loads(row["payload"]) if row is not None else {}
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    if task is None or task["merged_sha"]:
        return None
    record = load(observed)
    delivery = load(delivered)
    if not record.get("pr") and not (delivery.get("pr_url") or delivery.get("pr_exists")):
        return None
    now = now or datetime.now(UTC)
    limit = _refresh_after() if refresh_after is None else refresh_after
    age = _stamp_age(observed["created_at"], now) if observed is not None else None
    fresh_enough = bool(record.get("pr")) and age is not None and age <= limit
    # The record only changes when the pull request does, so an open one that sits
    # still keeps an old record; the last forge read in this process counts as fresh.
    key = (str(db_path()), task_id)
    asked = _asked.get(key)
    if not fresh_enough and asked is not None and now.timestamp() - asked[0] <= limit:
        record, fresh_enough = asked[1], True
    if not fresh_enough:
        fresh = _read_forge(task, repo)
        if fresh is not None:
            _asked[key] = (now.timestamp(), fresh)
            record = fresh
            if fresh.get("pr") is not None:
                _observe(task_id, fresh)
        elif not record.get("pr"):
            where = delivery.get("pr_url") or "its pull request"
            return HeldByPullRequest(
                None, f"kept: {where} exists and its state could not be read from the forge"
            )
    state = str(record.get("state") or "").upper()
    if record.get("pr") is None or record.get("merged") or state in ("MERGED", "CLOSED"):
        return None  # no pull request for the branch after all, or it is over
    number = record.get("pr")
    return HeldByPullRequest(number, f"kept: PR #{number} open")


def _read_forge(task: Any, repo: Any) -> dict[str, Any] | None:
    """The forge's entry for the task's branch, or ``None`` when it could not be asked."""
    if task is None or not task["branch"]:
        return None
    cwd = next(
        (
            p
            for p in (task["worktree_path"], repo["local_path"] if repo is not None else None)
            if p and Path(p).is_dir()
        ),
        None,
    )
    reader = lookup_pr
    if reader is None:
        from papaya_agent_runtime import watch

        reader = watch._lookup_pr
    try:
        entry = reader(str(task["branch"]), cwd)
    except Exception:  # noqa: BLE001 - a forge that cannot be asked is unknown
        return None
    return entry if isinstance(entry, dict) and entry.get("known") else None


def _observe(task_id: int, entry: dict[str, Any]) -> None:
    """Write the forge read as the PR watch record, in `rounds.observe_pr`'s shape."""
    import json

    from papaya_agent_runtime import team

    observed = {
        "task_id": task_id,
        "pr": entry.get("pr"),
        "url": entry.get("url"),
        "state": entry.get("state"),
        "merged": bool(entry.get("merged")),
        "ci": entry.get("ci"),
        "review": entry.get("review") or None,
        "head": entry.get("head"),
    }
    try:
        conn = init_db()
        try:
            row = conn.execute(
                "SELECT payload FROM events WHERE task_id = ? AND kind = ? "
                "ORDER BY id DESC LIMIT 1",
                (task_id, team.PR_OBSERVED_EVENT),
            ).fetchone()
            if row is not None and json.loads(row["payload"]) == observed:
                return
            task = store.get_task(conn, task_id)
            store.append_event(
                conn,
                kind=team.PR_OBSERVED_EVENT,
                payload=observed,
                run_id=task["run_id"] if task is not None else None,
                task_id=task_id,
            )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - the record is a cache; the answer stands without it
        pass


def _git(args: list[str], cwd: str) -> tuple[int, str]:
    proc = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout.strip()


def directory_size(path: str) -> int:
    """Bytes on disk under ``path``. Uses ``du`` and falls back to a walk."""
    proc = subprocess.run(["du", "-sk", path], capture_output=True, text=True, check=False)
    if proc.returncode == 0 and proc.stdout.strip():
        try:
            return int(proc.stdout.split()[0]) * 1024
        except (ValueError, IndexError):
            pass
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def _inspect(entry: WorktreeEntry) -> WorktreeEntry:
    """Fill in the disk facts and decide whether this slot can be reclaimed."""
    if not Path(entry.path).exists():
        entry.exists = False
        entry.reclaimable = True
        entry.reason = "worktree directory is already gone; only the lease record remains"
        return entry

    if entry.orphaned and not entry.managed:
        # Somebody else's worktree. A pool root is shared: the same treehouse home
        # holds this instance's pools *and* the user's own for the same repository,
        # told apart only by the hash in the directory name. Ownership is settled by
        # the checkout's base clone, never by the directory it sits in. Not sized
        # either — walking someone else's pool with `du` buys nothing.
        entry.reason = "not managed by this instance, leaving it alone"
        return entry

    entry.size_bytes = directory_size(entry.path)
    rc, out = _git(["status", "--porcelain"], entry.checkout)
    if rc != 0:
        entry.reason = "not a readable git worktree; leaving it alone"
        return entry
    entry.dirty = bool(out.strip())

    rc, out = _git(["rev-list", "--count", "HEAD", "--not", "--remotes"], entry.checkout)
    entry.unpushed_commits = int(out) if rc == 0 and out.isdigit() else -1

    if (
        not entry.orphaned or entry.task_id is not None
    ) and entry.task_status not in RECLAIMABLE_STATUSES:
        entry.reason = f"task is {entry.task_status or 'unknown'}, not finished with"
        if entry.orphaned:
            entry.reason = (
                f"no active lease, but task {entry.task_id} ({entry.task_status}) still names "
                "this slot as its worktree and can be resumed into it"
            )
        return entry
    if entry.task_id is not None and entry.task_status == "delivered":
        held = open_pull_request(entry.task_id)
        if held is not None:
            entry.open_pr = held.pr
            entry.reason = held.reason
            return entry
    if entry.dirty:
        entry.reason = "uncommitted changes in the worktree"
        return entry
    if entry.unpushed_commits != 0:
        count = "unknown" if entry.unpushed_commits < 0 else entry.unpushed_commits
        entry.reason = f"{count} commit(s) exist only here — nothing on a remote holds them"
        return entry
    entry.reclaimable = True
    entry.reason = (
        "no active lease owns this slot, clean, every commit is on a remote"
        if entry.orphaned
        else f"task {entry.task_status}, clean, every commit is on a remote"
    )
    return entry


# --------------------------------------------------------------------------- #
# Pool directories on disk — the slots no lease in this database owns
# --------------------------------------------------------------------------- #


def _resolved(path: str | Path) -> str:
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(path)


def _pool_root_under(path: str, home: Path) -> Path | None:
    """The pool root a recorded worktree path sits in, if it is under ``home``."""
    try:
        rel = Path(path).relative_to(home)
    except ValueError:
        return None
    return home / rel.parts[0] if rel.parts else None


def pool_roots(conn: sqlite3.Connection | None = None) -> list[Path]:
    """Every directory that hands out worktree slots on this machine.

    Three sources, because no single one is complete: the plain pool this process
    creates, the treehouse pool root for each registered repo (treehouse names it
    ``<repo>-<hash>``, so it is matched by prefix), and the root implied by any
    worktree path this database has ever recorded — which is how slots left by an
    earlier ppy instance are found at all.
    """
    conn = conn or init_db()
    home = treehouse_home()
    roots: list[Path] = []
    seen: set[str] = set()

    def offer(candidate: Path) -> None:
        key = _resolved(candidate)
        if key in seen:
            return
        seen.add(key)
        if candidate.is_dir():
            roots.append(candidate)

    offer(worktree_pools_dir())
    for repo_row in store.list_repos(conn):
        with contextlib.suppress(OSError):
            for match in sorted(home.glob(f"{repo_row['name']}-*")):
                offer(match)
    for row in conn.execute("SELECT worktree_path FROM leases").fetchall():
        root = _pool_root_under(row["worktree_path"] or "", home)
        if root is not None:
            offer(root)
    return roots


def _is_checkout(path: Path) -> bool:
    return (path / ".git").exists()


def _slot_checkout(slot: Path) -> Path | None:
    """The git checkout in a pool slot: the slot itself, or the one directory inside it."""
    if _is_checkout(slot):
        return slot
    try:
        children = sorted(p for p in slot.iterdir() if p.is_dir())
    except OSError:
        return None
    for child in children:
        if _is_checkout(child):
            return child
    return None


def _main_repo_of(checkout: str) -> str:
    """The base clone a linked worktree belongs to, or "" when it cannot be told."""
    rc, out = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], checkout)
    if rc != 0 or not out:
        rc, out = _git(["rev-parse", "--git-common-dir"], checkout)
        if rc != 0 or not out:
            return ""
        out = str((Path(checkout) / out).resolve()) if not Path(out).is_absolute() else out
    common = Path(out)
    return _resolved(common.parent if common.name == ".git" else common)


def _branch_of(checkout: str) -> str:
    rc, out = _git(["rev-parse", "--abbrev-ref", "HEAD"], checkout)
    return out if rc == 0 else ""


def managed_base_clones(conn: sqlite3.Connection) -> dict[str, str]:
    """Base clone path -> repo name, for the clones this instance actually owns.

    Only clones under ``.ppy/repos/`` count, which is where ``ppy repo add`` puts
    every one of them. This map is the whole ownership test for an orphan.
    """
    root = _resolved(repos_dir())
    out: dict[str, str] = {}
    for row in store.list_repos(conn):
        path = _resolved(row["local_path"])
        if path == root or path.startswith(root + os.sep):
            out[path] = row["name"]
    return out


def orphan_slots(
    repo: str | None = None, *, conn: sqlite3.Connection | None = None
) -> list[WorktreeEntry]:
    """Slot directories on disk that no active lease owns, inspected like any other.

    A directory is only reported when it is recognisably a checkout, so an
    unrelated file someone dropped in a pool root is never a prune candidate.

    Sitting in a pool root does **not** make a slot ours. A treehouse home holds
    this instance's pools next to the user's own for the same repositories —
    ``papaya-backend-monorepo-58a49b`` beside ``papaya-backend-monorepo-e5cc8b`` —
    and both match the same name glob. So ownership is decided by the checkout's
    base clone: only a slot whose main repository is one of this instance's base
    clones under ``.ppy/repos/`` is ever a candidate. Anything else is marked
    unmanaged and can never be reclaimed, however clean and however pushed.
    """
    conn = conn or init_db()
    owned = set()
    for row in conn.execute("SELECT worktree_path FROM leases WHERE status = 'active'").fetchall():
        path = row["worktree_path"] or ""
        owned.add(_resolved(path))
        owned.add(_resolved(Path(path).parent))
    # A slot no lease owns can still be the recorded worktree of a task that is
    # not over — a failed worker whose lease was handed back while its task is
    # still `ppy resume`-able. Task 158 lost its checkout that way on 2026-09-06
    # (issue #58). Such a slot is listed, with the task named, and never taken.
    resumable: dict[str, tuple[int, str]] = {}
    for row in conn.execute(
        "SELECT id, status, worktree_path FROM tasks WHERE worktree_path IS NOT NULL "
        "AND status NOT IN (?, ?, ?)",
        RECLAIMABLE_STATUSES,
    ).fetchall():
        for key in (_resolved(row["worktree_path"]), _resolved(Path(row["worktree_path"]).parent)):
            resumable.setdefault(key, (int(row["id"]), row["status"]))
    # A delivered task that has not merged may still need its slot for its pull
    # request; `_inspect` asks whether that pull request is open. Newest task first,
    # because pool paths are recycled.
    delivered: dict[str, int] = {}
    for row in conn.execute(
        "SELECT id, worktree_path FROM tasks WHERE worktree_path IS NOT NULL "
        "AND status = 'delivered' AND (merged_sha IS NULL OR merged_sha = '') ORDER BY id DESC"
    ).fetchall():
        for key in (_resolved(row["worktree_path"]), _resolved(Path(row["worktree_path"]).parent)):
            delivered.setdefault(key, int(row["id"]))
    repos_by_path = managed_base_clones(conn)

    entries: list[WorktreeEntry] = []
    for root in pool_roots(conn):
        try:
            children = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            continue
        for slot in children:
            if _resolved(slot) in owned:
                continue
            checkout = _slot_checkout(slot)
            if checkout is None or _resolved(checkout) in owned:
                continue
            owner = repos_by_path.get(_main_repo_of(str(checkout)))
            if repo is not None and owner != repo:
                continue
            named_by = resumable.get(_resolved(slot)) or resumable.get(_resolved(checkout))
            if named_by is None:
                shipped = delivered.get(_resolved(slot)) or delivered.get(_resolved(checkout))
                named_by = (shipped, "delivered") if shipped is not None else None
            entry = WorktreeEntry(
                lease_id="",
                path=str(slot),
                repo=owner,
                task_id=named_by[0] if named_by else None,
                task_status=named_by[1] if named_by else None,
                branch=_branch_of(str(checkout)),
                orphaned=True,
                checkout_path=str(checkout),
                managed=owner is not None,
            )
            entries.append(_inspect(entry))
    return entries


def remove_orphan_slot(entry: WorktreeEntry) -> None:
    """Give an unowned slot back: unregister the worktree, then take the directory.

    The base clone is asked to drop the worktree first so its administrative files
    go too; the slot directory itself is removed afterwards because a treehouse
    slot is a numbered wrapper around the checkout and would otherwise stay.

    The ownership check is repeated here rather than trusted from the caller: this
    is the one function that deletes a directory nobody recorded, and the cost of
    being wrong is the user's own uncommitted-or-not work.
    """
    if not entry.managed:
        raise LeaseError(
            f"refusing to remove {entry.path}: it is not a worktree of a base clone "
            "this instance manages"
        )
    main = _main_repo_of(entry.checkout)
    if main:
        rc, _out = _git(["worktree", "remove", "--force", entry.checkout], main)
        if rc != 0:
            _git(["worktree", "prune"], main)
    if Path(entry.path).exists():
        shutil.rmtree(entry.path, ignore_errors=True)
    if Path(entry.path).exists():
        raise LeaseError(f"{entry.path} is still on disk")


def list_worktrees(
    repo: str | None = None, *, include_orphans: bool = True, task_id: int | None = None
) -> list[WorktreeEntry]:
    """Every leased slot and every orphaned one, with task, cleanliness, and size.

    Leases come first (newest first), then the slots on disk that no active lease
    owns. A failure to walk the pools never costs the caller the lease inventory —
    ``ppy dispatch`` asks this question before every dispatch. ``task_id`` narrows
    the inventory to that one task's slot, and then no pool is walked at all.
    """
    if task_id is not None:
        return [e for e in list_worktrees(repo, include_orphans=False) if e.task_id == task_id]
    conn = init_db()
    rows = conn.execute(
        """
        SELECT l.id AS lease_id, l.worktree_path, l.branch, l.task_id, l.backend,
               r.name AS repo_name, r.local_path AS repo_path, t.status AS task_status
        FROM leases l
        LEFT JOIN repos r ON r.id = l.repo_id
        LEFT JOIN tasks t ON t.id = l.task_id
        WHERE l.status = 'active'
        ORDER BY l.created_at DESC
        """
    ).fetchall()
    entries = []
    for row in rows:
        if repo is not None and row["repo_name"] != repo:
            continue
        entries.append(
            _inspect(
                WorktreeEntry(
                    lease_id=row["lease_id"],
                    path=row["worktree_path"],
                    repo=row["repo_name"],
                    task_id=row["task_id"],
                    task_status=row["task_status"],
                    branch=row["branch"],
                    backend=row["backend"],
                    repo_path=row["repo_path"] or "",
                )
            )
        )
    if include_orphans:
        # A pool walk crosses directories nobody here owns; it must never cost the
        # caller the lease inventory it actually asked for.
        with contextlib.suppress(Exception):
            entries.extend(orphan_slots(repo, conn=conn))
    return entries


def reclaimable_bytes(repo: str | None = None) -> int:
    """How much `ppy worktree prune` would free right now. Never raises."""
    try:
        return sum(e.size_bytes for e in list_worktrees(repo) if e.reclaimable)
    except Exception:  # noqa: BLE001 - an inventory failure must not break a caller
        return 0


def is_managed_clone(path: str | None) -> bool:
    """Whether ``path`` is a base clone under this instance's ``.ppy/repos/``."""
    if not path:
        return False
    root = _resolved(repos_dir())
    resolved = _resolved(path)
    return resolved == root or resolved.startswith(root + os.sep)


def prune(
    repo: str | None = None,
    *,
    dry_run: bool = False,
    task_id: int | None = None,
    managed_only: bool = False,
) -> dict:
    """Remove the worktrees of finished tasks that hold nothing unique.

    Returns what went, what stayed and why, and the bytes involved. ``dry_run``
    changes nothing on disk or in the database. ``task_id`` prunes that one
    task's slot only. ``managed_only`` also holds a *leased* slot to the orphans'
    ownership rule — its base clone must be under ``.ppy/repos/`` — which is what
    the manager's unattended hygiene asks for: nothing it did not clone is its to
    remove, even through a lease.
    """
    from papaya_agent_runtime import compose

    conn = init_db()
    entries = list_worktrees(repo, task_id=task_id)
    removed: list[dict] = []
    skipped: list[dict] = []
    stacks: list[dict] = []
    for entry in entries:
        if (
            managed_only
            and not entry.orphaned
            and entry.reclaimable
            and not is_managed_clone(entry.repo_path)
        ):
            entry.reclaimable = False
            entry.managed = False
            entry.reason = "its base clone is not under .ppy/repos, leaving it alone"
        record = {
            "lease": entry.lease_id,
            "task_id": entry.task_id,
            "task_status": "orphaned" if entry.orphaned else entry.task_status,
            "path": entry.path,
            "branch": entry.branch,
            "repo": entry.repo,
            "size_bytes": entry.size_bytes,
            "reason": entry.reason,
            "orphaned": entry.orphaned,
            "managed": entry.managed,
            "repo_path": entry.repo_path,
            "open_pr": entry.open_pr,
        }
        if not entry.reclaimable:
            record["dirty"] = entry.dirty
            record["unpushed_commits"] = entry.unpushed_commits
            skipped.append(record)
            continue
        if dry_run:
            removed.append(record)
            continue
        if entry.orphaned:
            # No lease to release and no task to stamp: the directory is the whole
            # record, so removing it is the whole operation.
            try:
                remove_orphan_slot(entry)
            except (LeaseError, OSError) as exc:
                record["reason"] = f"could not remove: {exc}"
                skipped.append(record)
                continue
            removed.append(record)
            store.append_event(conn, kind="worktree_pruned", payload=record)
            continue
        lease = Lease(
            id=entry.lease_id,
            repo_id=None,
            task_id=entry.task_id,
            branch=entry.branch,
            worktree_path=entry.path,
            base_sha=None,
            backend=entry.backend,
        )
        try:
            # The branch is kept: every commit on it is already on a remote, and a
            # kept branch costs nothing while a deleted one cannot be undone.
            LeaseManager(lease.backend).release(
                lease, repo_path=entry.repo_path, remove_branch=False
            )
        except LeaseError as exc:
            record["reason"] = f"could not remove: {exc}"
            skipped.append(record)
            continue
        removed.append(record)
        task = store.get_task(conn, entry.task_id) if entry.task_id else None
        store.append_event(
            conn,
            kind="worktree_pruned",
            payload=record,
            run_id=task["run_id"] if task else None,
            task_id=entry.task_id,
        )
        # The slot is gone, so the per-task database that ran alongside it has
        # nothing left to serve. Reclaiming disk and leaving the stack up was how
        # fifteen of them accumulated until Docker ran out of networks.
        torn_down = compose.teardown_for_task(entry.task_id, trigger="worktree prune", conn=conn)
        if torn_down:
            stacks.append(torn_down)

    return {
        "dry_run": dry_run,
        "repo": repo,
        "removed": removed,
        "skipped": skipped,
        "compose": stacks,
        "reclaimed_bytes": sum(r["size_bytes"] for r in removed),
        "held_bytes": sum(r["size_bytes"] for r in skipped),
    }


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"
