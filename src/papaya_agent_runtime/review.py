"""Review bundles and the exact-HEAD review gate.

A review is bound to the precise commit SHA it examined. Delivery refuses unless
an approved review is bound to the worktree's current head, so a post-review
change can never be silently shipped.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime

from papaya_agent_runtime.state import init_db, store


class ReviewError(Exception):
    pass


@dataclass
class ReviewBundle:
    task_id: int
    base_sha: str
    head_sha: str
    diffstat: str
    files_changed: int
    #: Where ``base_sha`` came from, in words: what the diff was taken against.
    base_from: str = ""


#: How long the fetch before a review may take; a slow forge falls back, it never hangs.
FETCH_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class ReviewBase:
    """The commit a review diffs from, and how it was found."""

    sha: str
    #: The branch it is the merge-base with (``forge/main``), or "" for the dispatch base.
    ref: str
    #: One line saying which base was used and why, for `review show` and the review turn.
    how: str


def _git(args: list[str], cwd: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise ReviewError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _git_quiet(args: list[str], cwd: str, timeout: float = 30) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=False, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def head_sha(worktree_path: str) -> str:
    return _git(["rev-parse", "HEAD"], cwd=worktree_path)


def review_base(conn, task) -> ReviewBase:
    """The merge-base of the worker's HEAD with the branch its pull request targets.

    The dispatch-time ``base_sha`` stops being the worker's starting point the moment
    the branch is rebased: on PAP-222 (2026-09-17) `ppy review show 21` diffed from
    it after a rebase onto a newer main and listed 51 files instead of the worker's
    3. So the review asks the forge for the target branch (the stacked parent, else
    the repository's default) and diffs from where HEAD meets it — a rebase moves
    both ends together and never changes what is reviewed. When the forge cannot be
    asked, the last fetched copy is used and said; with neither, the dispatch base.
    """
    worktree = task["worktree_path"]
    dispatch = task["base_sha"] or ""
    fallback = ReviewBase(dispatch, "", f"dispatch-time base {dispatch[:8]}")
    repo = (
        conn.execute("SELECT * FROM repos WHERE id = ?", (task["repo_id"],)).fetchone()
        if task["repo_id"]
        else None
    )
    stacked = task["stacked_on"] if "stacked_on" in task.keys() else None  # noqa: SIM118 - sqlite3.Row
    target = stacked or (repo["default_branch"] if repo is not None else None) or "main"
    remote = "origin"
    if repo is not None:
        from papaya_agent_runtime import repos

        try:
            remote = repos.upstream_remote(repo)
        except repos.RepoError:
            remote = "origin"
    tracking = f"refs/remotes/{remote}/{target}"
    fetched = (
        _git_quiet(
            ["fetch", "--quiet", remote, f"+refs/heads/{target}:{tracking}"],
            cwd=worktree,
            timeout=FETCH_TIMEOUT_SECONDS,
        )
        is not None
    )
    sha = _git_quiet(["merge-base", "HEAD", tracking], cwd=worktree)
    if not sha:
        if dispatch:
            why = "fetched, but HEAD shares no history with it" if fetched else "could not fetch"
            return ReviewBase(
                dispatch, "", f"dispatch-time base {dispatch[:8]} ({remote}/{target}: {why})"
            )
        return fallback
    said = "fetched now" if fetched else "could not fetch; the last fetched copy"
    return ReviewBase(sha, f"{remote}/{target}", f"merge-base with {remote}/{target} ({said})")


def build_bundle(task_id: int) -> ReviewBundle:
    conn = init_db()
    task = store.get_task(conn, task_id)
    if task is None:
        raise ReviewError(f"task {task_id} not found")
    worktree = task["worktree_path"]
    if not worktree or not task["base_sha"]:
        raise ReviewError("task has no worktree/base to review")
    head = head_sha(worktree)
    found = review_base(conn, task)
    base = found.sha
    diffstat = _git(["diff", "--stat", f"{base}..{head}"], cwd=worktree)
    names = _git(["diff", "--name-only", f"{base}..{head}"], cwd=worktree)
    files = len([n for n in names.splitlines() if n.strip()])
    return ReviewBundle(task_id, base, head, diffstat, files, base_from=found.how)


def review_base_line(task_id: int) -> str:
    """`review_base` as the review turn's fact; "" when the task has no worktree. Never raises."""
    conn = init_db()
    try:
        task = store.get_task(conn, task_id)
        if task is None or not task["worktree_path"]:
            return ""
        from pathlib import Path

        if not Path(task["worktree_path"]).is_dir():
            return ""
        found = review_base(conn, task)
        head = head_sha(task["worktree_path"])
        return f"{found.sha[:8]}..{head[:8]}: {found.how}"
    except Exception:  # noqa: BLE001 - a fact that cannot be read is left out
        return ""
    finally:
        conn.close()


def record_review(
    task_id: int,
    verdict: str,
    findings: str = "",
    *,
    head: str | None = None,
    note: str = "",
) -> dict:
    """Record a verdict against the exact commit it examined.

    ``note`` is the reviewer's own words about *this* approval — what they checked,
    which captures they opened, what they accepted a caveat on. It rides the review
    row, so it is bound to the same SHA as the verdict and cannot be mistaken for a
    note about some later commit; ``ppy review status``/``show`` print it back. The
    pull request's text is the separate description (:mod:`papaya_agent_runtime.pr_body`).
    """
    if verdict not in ("approved", "changes_requested"):
        raise ReviewError("verdict must be 'approved' or 'changes_requested'")
    conn = init_db()
    task = store.get_task(conn, task_id)
    if task is None:
        raise ReviewError(f"task {task_id} not found")
    head = head or head_sha(task["worktree_path"])
    note = (note or "").strip()
    conn.execute(
        """
        INSERT INTO reviews (task_id, base_sha, head_sha, verdict, findings, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (task_id, task["base_sha"], head, verdict, findings, note, datetime.now(UTC).isoformat()),
    )
    conn.commit()
    store.append_event(
        conn,
        kind="review_requested" if verdict == "changes_requested" else "reviewed",
        payload={"task_id": task_id, "verdict": verdict, "head_sha": head, "note": note},
        run_id=task["run_id"],
        task_id=task_id,
    )
    from papaya_agent_runtime import standalone

    standalone.skip_if_local(conn, task_id, standalone.REVIEWED)
    return {"task_id": task_id, "verdict": verdict, "head_sha": head, "note": note}


def latest_review(task_id: int):
    conn = init_db()
    return conn.execute(
        "SELECT * FROM reviews WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,)
    ).fetchone()


def approval_note(task_id: int) -> str:
    """The note on the latest approval, or "" when there is none.

    Only an approval's note is returned: a note left with a changes-requested verdict
    describes work that has since moved on, and quoting it as verification would be
    a lie.
    """
    review = latest_review(task_id)
    if review is None or review["verdict"] != "approved":
        return ""
    if "note" not in review.keys():  # noqa: SIM118 - sqlite3.Row: `in` scans values
        return ""
    return (review["note"] or "").strip()


def is_approved_at_head(task_id: int) -> tuple[bool, str]:
    """Return (approved, reason). Approved only if the latest approval SHA == head."""
    conn = init_db()
    task = store.get_task(conn, task_id)
    if task is None:
        return False, "task not found"
    review = latest_review(task_id)
    if review is None:
        return False, "no review recorded"
    if review["verdict"] != "approved":
        return False, f"latest review verdict is {review['verdict']}"
    current = head_sha(task["worktree_path"])
    if review["head_sha"] != current:
        return False, (
            f"review bound to {review['head_sha'][:8]} but head is {current[:8]}; "
            "re-review required"
        )
    return True, "approved at current head"
