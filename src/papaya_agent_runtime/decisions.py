"""Durable, scoped decisions and reuse.

Decisions the user (or manager) makes are recorded so the same routine question
is not asked twice. Each decision has a scope (task | run | global) and a
fingerprint of the normalized question, and can be superseded when context
changes. Autonomous question routing consults these before escalating.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass

SCOPES = ("task", "run", "global")


def fingerprint(question: str) -> str:
    normalized = re.sub(r"\s+", " ", question.strip().lower())
    normalized = re.sub(r"[^\w\s?]", "", normalized)
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


@dataclass
class Decision:
    id: int
    question: str
    answer: str
    scope: str
    run_id: int | None
    task_id: int | None
    fingerprint: str
    context: str | None = None


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def record_decision(
    conn: sqlite3.Connection,
    *,
    question: str,
    answer: str,
    scope: str = "run",
    run_id: int | None = None,
    task_id: int | None = None,
    rationale: str | None = None,
    author: str = "user",
    context: str | None = None,
) -> int:
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")

    fp = fingerprint(question)
    cur = conn.execute(
        """
        INSERT INTO decisions
            (run_id, task_id, question, answer, rationale, scope, author, standing,
             fingerprint, context, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id if scope != "global" else None,
            task_id if scope == "task" else None,
            question,
            answer,
            rationale,
            scope,
            author,
            1 if scope == "global" else 0,
            fp,
            context,
            _now(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _row_to_decision(row: sqlite3.Row) -> Decision:
    keys = row.keys()
    return Decision(
        id=row["id"],
        question=row["question"],
        answer=row["answer"],
        scope=row["scope"],
        run_id=row["run_id"],
        task_id=row["task_id"],
        fingerprint=row["fingerprint"],
        context=row["context"] if "context" in keys else None,
    )


class StaleDecision(Exception):
    """A matching decision exists but its recorded premise no longer holds."""

    def __init__(self, decision: Decision, current_context: str | None) -> None:
        super().__init__(
            f"decision {decision.id} was recorded under a different premise; "
            "confirm the changed context before reuse"
        )
        self.decision = decision
        self.current_context = current_context


def find_matching(
    conn: sqlite3.Connection,
    question: str,
    *,
    run_id: int | None = None,
    task_id: int | None = None,
    context: str | None = None,
) -> Decision | None:
    """Return the most relevant live decision for a question, or None.

    Precedence: global > run (same run) > task (same task); most specific/recent
    wins. Superseded and invalidated decisions are ignored. If ``context`` is
    given and the winning decision was recorded under a *different* non-null
    context (a changed premise), raise :class:`StaleDecision` so the caller asks
    only for the delta rather than silently reusing a timeless preference.
    """
    fp = fingerprint(question)
    rows = conn.execute(
        "SELECT * FROM decisions WHERE fingerprint = ? AND superseded_by IS NULL "
        "AND invalidated_at IS NULL ORDER BY id DESC",
        (fp,),
    ).fetchall()
    best: sqlite3.Row | None = None
    best_rank = -1
    rank = {"global": 3, "run": 2, "task": 1}
    for row in rows:
        scope = row["scope"]
        if scope == "run" and row["run_id"] != run_id:
            continue
        if scope == "task" and row["task_id"] != task_id:
            continue
        if rank[scope] > best_rank:
            best = row
            best_rank = rank[scope]
    if best is None:
        return None
    decision = _row_to_decision(best)
    if context is not None and decision.context is not None and decision.context != context:
        raise StaleDecision(decision, context)
    return decision


def supersede(conn: sqlite3.Connection, decision_id: int, by_id: int) -> None:
    conn.execute("UPDATE decisions SET superseded_by = ? WHERE id = ?", (by_id, decision_id))
    conn.commit()


def invalidate(conn: sqlite3.Connection, decision_id: int) -> None:
    """Mark a decision no longer valid without erasing history."""
    conn.execute("UPDATE decisions SET invalidated_at = ? WHERE id = ?", (_now(), decision_id))
    conn.commit()


def forget(conn: sqlite3.Connection, decision_id: int) -> None:
    """Hard-delete a decision (user-requested; the audit trail loses this row)."""
    conn.execute("DELETE FROM decisions WHERE id = ?", (decision_id,))
    conn.commit()


def list_decisions(conn: sqlite3.Connection, *, include_inactive: bool = False) -> list[dict]:
    where = "" if include_inactive else "WHERE superseded_by IS NULL AND invalidated_at IS NULL"
    rows = conn.execute(f"SELECT * FROM decisions {where} ORDER BY id").fetchall()
    out = []
    for r in rows:
        keys = r.keys()
        out.append(
            {
                "id": r["id"],
                "scope": r["scope"],
                "question": r["question"],
                "answer": r["answer"],
                "run_id": r["run_id"],
                "task_id": r["task_id"],
                "superseded_by": r["superseded_by"],
                "invalidated": bool(r["invalidated_at"]) if "invalidated_at" in keys else False,
            }
        )
    return out
