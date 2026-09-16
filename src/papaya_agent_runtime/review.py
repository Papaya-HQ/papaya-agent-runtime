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


def _git(args: list[str], cwd: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise ReviewError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def head_sha(worktree_path: str) -> str:
    return _git(["rev-parse", "HEAD"], cwd=worktree_path)


def build_bundle(task_id: int) -> ReviewBundle:
    conn = init_db()
    task = store.get_task(conn, task_id)
    if task is None:
        raise ReviewError(f"task {task_id} not found")
    worktree = task["worktree_path"]
    base = task["base_sha"]
    if not worktree or not base:
        raise ReviewError("task has no worktree/base to review")
    head = head_sha(worktree)
    diffstat = _git(["diff", "--stat", f"{base}..{head}"], cwd=worktree)
    names = _git(["diff", "--name-only", f"{base}..{head}"], cwd=worktree)
    files = len([n for n in names.splitlines() if n.strip()])
    return ReviewBundle(task_id, base, head, diffstat, files)


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
    note about some later commit; ``ppy review status``/``show`` print it back, and
    delivery quotes it in the pull request's verification section.
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
