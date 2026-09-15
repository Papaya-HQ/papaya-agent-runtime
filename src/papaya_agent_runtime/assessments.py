"""Proactive, evidence-backed performance reviews for the runtime manager.

The control plane owns cadence, evidence, persistence, and guardrails.  The
manager model owns the judgment: it turns a bounded scorecard into one to three
measurable experiments and then aligns those proposals with the user.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from papaya_agent_runtime import memory
from papaya_agent_runtime.config import AssessmentPolicy, ConfigError, load_config
from papaya_agent_runtime.state import init_db, store

OPEN_STATUSES = ("ready", "awaiting_user")
ALIGNMENT_DECISIONS = ("approved", "revised", "dismissed")
STRUCTURAL_CATEGORIES = {"authority", "config", "framework", "safety"}


class AssessmentError(Exception):
    """Raised when an assessment transition or report is invalid."""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def current_policy() -> AssessmentPolicy:
    """Load the configured policy, falling back to safe defaults pre-setup."""
    try:
        return load_config().assessments
    except ConfigError:
        return AssessmentPolicy()


def latest_cycle(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM assessment_cycles ORDER BY id DESC LIMIT 1").fetchone()


def get_cycle(conn: sqlite3.Connection, cycle_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM assessment_cycles WHERE id = ?", (cycle_id,)).fetchone()


def list_actions(conn: sqlite3.Connection, cycle_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM improvement_actions WHERE assessment_id = ? ORDER BY id",
            (cycle_id,),
        ).fetchall()
    )


def _window_start(conn: sqlite3.Connection, now: datetime) -> datetime:
    last = latest_cycle(conn)
    if last is not None:
        return _parse(last["created_at"]) or now
    row = conn.execute(
        """
        SELECT MIN(created_at) AS first_at FROM (
            SELECT created_at FROM runs
            UNION ALL
            SELECT created_at FROM events
        )
        """
    ).fetchone()
    return _parse(row["first_at"] if row else None) or now


def collect_evidence(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, Any]:
    """Build a deterministic scorecard since the previous assessment."""
    now = now or _now()
    start = _window_start(conn, now)
    start_iso = _iso(start)

    completed = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM runs r
        WHERE r.created_at >= ?
          AND (
            r.status IN ('complete', 'completed', 'done', 'delivered')
            OR (
              EXISTS (SELECT 1 FROM tasks t WHERE t.run_id = r.id)
              AND NOT EXISTS (
                SELECT 1 FROM tasks t
                WHERE t.run_id = r.id
                  AND t.status NOT IN ('worker_done', 'delivered', 'closed', 'cancelled')
              )
            )
          )
        """,
        (start_iso,),
    ).fetchone()["n"]
    tasks = conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN status IN ('worker_done','delivered') THEN 1 ELSE 0 END) AS succeeded,
               SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
               SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END) AS blocked,
               AVG((julianday(updated_at) - julianday(created_at)) * 86400.0) AS avg_seconds
        FROM tasks WHERE created_at >= ?
        """,
        (start_iso,),
    ).fetchone()
    usage = conn.execute(
        """
        SELECT COUNT(*) AS calls,
               COALESCE(SUM(input_tokens), 0) AS input_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens
        FROM usage WHERE created_at >= ?
        """,
        (start_iso,),
    ).fetchone()
    reviews = conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN verdict = 'approved' THEN 1 ELSE 0 END) AS approved,
               SUM(CASE WHEN verdict = 'changes_requested' THEN 1 ELSE 0 END) AS rework
        FROM reviews WHERE created_at >= ?
        """,
        (start_iso,),
    ).fetchone()
    event_rows = conn.execute(
        """
        SELECT kind, COUNT(*) AS n FROM events
        WHERE created_at >= ?
        GROUP BY kind ORDER BY kind
        """,
        (start_iso,),
    ).fetchall()
    event_counts = {row["kind"]: int(row["n"]) for row in event_rows}
    decisions = conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN author = 'user' THEN 1 ELSE 0 END) AS user_decisions
        FROM decisions WHERE created_at >= ?
        """,
        (start_iso,),
    ).fetchone()
    cross_repo = conn.execute(
        """
        SELECT COUNT(*) AS runs FROM (
            SELECT t.run_id FROM tasks t
            JOIN runs r ON r.id = t.run_id
            WHERE r.created_at >= ? AND t.repo_id IS NOT NULL
            GROUP BY t.run_id HAVING COUNT(DISTINCT t.repo_id) > 1
        )
        """,
        (start_iso,),
    ).fetchone()
    previous = latest_cycle(conn)
    previous_plan = None
    if previous is not None:
        previous_plan = {
            "assessment_id": int(previous["id"]),
            "status": previous["status"],
            "summary": previous["summary"],
            "user_response": json.loads(previous["user_response"])
            if previous["user_response"]
            else None,
            "actions": [
                {
                    "description": action["description"],
                    "category": action["category"],
                    "observation": action["observation"],
                    "likely_cause": action["likely_cause"],
                    "baseline": action["baseline"],
                    "target": action["target"],
                    "measurement": action["measurement"],
                    "status": action["status"],
                }
                for action in list_actions(conn, int(previous["id"]))
            ],
        }
    from papaya_agent_runtime import reflections

    reflection_entries = reflections.since(start_iso, conn=conn, limit=50)
    failed_work = max(int(tasks["failed"] or 0), event_counts.get("error", 0))
    failure_signals = failed_work + int(reviews["rework"] or 0) + event_counts.get("steer", 0)
    return {
        "window": {
            "start": start_iso,
            "end": _iso(now),
            "elapsed_days": max(0, (now - start).days),
        },
        "runs": {"completed": int(completed)},
        "tasks": {
            "total": int(tasks["total"] or 0),
            "succeeded": int(tasks["succeeded"] or 0),
            "failed": int(tasks["failed"] or 0),
            "blocked": int(tasks["blocked"] or 0),
            "average_duration_seconds": round(float(tasks["avg_seconds"] or 0), 2),
        },
        "reviews": {
            "total": int(reviews["total"] or 0),
            "approved": int(reviews["approved"] or 0),
            "changes_requested": int(reviews["rework"] or 0),
        },
        "usage": {
            "calls": int(usage["calls"] or 0),
            "input_tokens": int(usage["input_tokens"] or 0),
            "output_tokens": int(usage["output_tokens"] or 0),
        },
        "decisions": {
            "recorded": int(decisions["total"] or 0),
            "user_decisions": int(decisions["user_decisions"] or 0),
            "reused": event_counts.get("auto_answered", 0),
        },
        "cross_repo": {"runs": int(cross_repo["runs"] or 0)},
        "events": event_counts,
        "failure_signals": failure_signals,
        "reflections": {
            "count": len(reflection_entries),
            "entries": [
                {
                    "task_id": e["task_id"],
                    "title": e["title"],
                    "self": e["self"],
                    "manager": e["manager"],
                    "at": e["at"],
                }
                for e in reflection_entries
            ],
        },
        "previous_plan": previous_plan,
    }


def due_reason(
    conn: sqlite3.Connection,
    *,
    policy: AssessmentPolicy | None = None,
    now: datetime | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """Return the trigger name and current evidence, or ``None`` when not due."""
    policy = policy or current_policy()
    now = now or _now()
    evidence = collect_evidence(conn, now=now)
    if not policy.enabled:
        return None, evidence

    last = latest_cycle(conn)
    if last is not None and last["status"] in OPEN_STATUSES:
        return "pending", json.loads(last["evidence"])

    last_at = (
        _parse(last["aligned_at"] or last["completed_at"] or last["created_at"])
        if last is not None
        else None
    )
    if last_at is not None and now - last_at < timedelta(days=policy.cooldown_days):
        return None, evidence

    completed = evidence["runs"]["completed"]
    if evidence["failure_signals"] >= policy.failure_trigger_count:
        return "failure", evidence
    if completed >= policy.completed_runs:
        return "completed_runs", evidence
    if completed >= policy.minimum_runs and evidence["window"]["elapsed_days"] >= policy.max_days:
        return "elapsed_time", evidence
    return None, evidence


def ensure_due(
    conn: sqlite3.Connection | None = None,
    *,
    policy: AssessmentPolicy | None = None,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, Any] | None:
    """Create one durable ready cycle when policy says a review is due."""
    conn = conn or init_db()
    now = now or _now()
    policy = policy or current_policy()
    if not policy.enabled and not force:
        return None
    existing = latest_cycle(conn)
    if existing is not None and existing["status"] in OPEN_STATUSES:
        return cycle_dict(conn, int(existing["id"]))

    reason, evidence = due_reason(conn, policy=policy, now=now)
    if not force and reason is None:
        return None
    trigger = "manual" if force and reason is None else str(reason)
    try:
        cur = conn.execute(
            """
            INSERT INTO assessment_cycles
                (trigger, status, window_start, window_end, evidence, created_at)
            VALUES (?, 'ready', ?, ?, ?, ?)
            """,
            (
                trigger,
                evidence["window"]["start"],
                evidence["window"]["end"],
                json.dumps(evidence, sort_keys=True),
                _iso(now),
            ),
        )
    except sqlite3.IntegrityError:
        # A hook and the supervisor may discover the same due boundary together.
        # The partial unique index elects one durable cycle.
        conn.rollback()
        concurrent = latest_cycle(conn)
        if concurrent is not None and concurrent["status"] in OPEN_STATUSES:
            return cycle_dict(conn, int(concurrent["id"]))
        raise
    cycle_id = int(cur.lastrowid)
    conn.commit()
    store.append_event(
        conn,
        kind="assessment_ready",
        payload={"assessment_id": cycle_id, "trigger": trigger},
    )
    return cycle_dict(conn, cycle_id)


def cycle_dict(conn: sqlite3.Connection, cycle_id: int) -> dict[str, Any]:
    row = get_cycle(conn, cycle_id)
    if row is None:
        raise AssessmentError(f"assessment {cycle_id} not found")
    data = dict(row)
    data["evidence"] = json.loads(data["evidence"])
    data["actions"] = [dict(action) for action in list_actions(conn, cycle_id)]
    return data


def assessment_prompt(cycle: dict[str, Any]) -> str:
    """Render bounded instructions for the manager model, not for the user."""
    evidence = json.dumps(cycle["evidence"], sort_keys=True)
    return (
        f"Periodic performance assessment {cycle['id']} is due ({cycle['trigger']}). "
        "Do it now without asking whether to begin. Inspect this deterministic evidence: "
        f"{evidence}. Weigh the workers' reflections in it (their self-assessments and their "
        "assessments of you — brief clarity, scope, steering, review) as first-class input, "
        "quoting them where they change a conclusion. Identify what improved, what "
        "underperformed, and the most likely root causes. Propose only 1-3 measurable "
        "experiments. You may autonomously adjust "
        "reversible operating practice (routing, sequencing, memory, caching); any change "
        "to authority, model/spend ceilings, config, safety policy, framework source, hooks, "
        "or skills requires explicit user approval. Record the assessment with `./bin/ppy "
        f"assessment complete {cycle['id']}` using `--summary`, strengths, weaknesses, and "
        "an `--action-json` list; every action needs description, observation, likely_cause, "
        "baseline, target, and measurement. Then "
        "give the user a short performance review with the proposed plan. After the user "
        "responds, record that alignment with `./bin/ppy assessment align`. Do not relitigate "
        "an aligned action unless its assumptions or surrounding context materially change."
    )


def complete(
    cycle_id: int,
    *,
    summary: str,
    strengths: list[str],
    weaknesses: list[str],
    actions: list[dict[str, Any]],
    conn: sqlite3.Connection | None = None,
    policy: AssessmentPolicy | None = None,
) -> dict[str, Any]:
    """Persist the manager's assessment and enter the user-alignment phase."""
    conn = conn or init_db()
    policy = policy or current_policy()
    row = get_cycle(conn, cycle_id)
    if row is None:
        raise AssessmentError(f"assessment {cycle_id} not found")
    if row["status"] != "ready":
        raise AssessmentError(f"assessment {cycle_id} is {row['status']}, not ready")
    if not summary.strip():
        raise AssessmentError("assessment summary cannot be empty")
    if not 1 <= len(actions) <= policy.max_actions:
        raise AssessmentError(f"assessment needs 1-{policy.max_actions} actions")

    normalized_actions: list[dict[str, Any]] = []
    for action in actions:
        description = str(action.get("description", "")).strip()
        if not description:
            raise AssessmentError("every action needs a description")
        for field in ("observation", "likely_cause", "baseline", "target", "measurement"):
            if not str(action.get(field, "")).strip():
                raise AssessmentError(f"every action needs a {field}")
        category = str(action.get("category", "practice")).strip().lower() or "practice"
        normalized_actions.append(
            {
                **action,
                "description": description,
                "category": category,
                "requires_approval": bool(action.get("requires_approval", False))
                or category in STRUCTURAL_CATEGORIES,
            }
        )

    now = _iso(_now())
    conn.execute(
        """
        UPDATE assessment_cycles
        SET status = 'awaiting_user', summary = ?, strengths = ?, weaknesses = ?,
            completed_at = ?
        WHERE id = ?
        """,
        (summary.strip(), json.dumps(strengths), json.dumps(weaknesses), now, cycle_id),
    )
    for action in normalized_actions:
        conn.execute(
            """
            INSERT INTO improvement_actions
                (assessment_id, description, category, observation, likely_cause,
                 baseline, target, measurement, requires_approval, status,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?)
            """,
            (
                cycle_id,
                action["description"],
                action["category"],
                action.get("observation"),
                action.get("likely_cause"),
                action.get("baseline"),
                action.get("target"),
                action.get("measurement"),
                int(action["requires_approval"]),
                now,
                now,
            ),
        )
    conn.commit()
    store.append_event(
        conn,
        kind="assessment_awaiting_user",
        payload={"assessment_id": cycle_id, "actions": len(actions)},
    )
    return cycle_dict(conn, cycle_id)


def align(
    cycle_id: int,
    *,
    decision: str,
    notes: str = "",
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Record the user's response and activate or retire the experiments."""
    conn = conn or init_db()
    if decision not in ALIGNMENT_DECISIONS:
        raise AssessmentError(f"decision must be one of {ALIGNMENT_DECISIONS}")
    row = get_cycle(conn, cycle_id)
    if row is None:
        raise AssessmentError(f"assessment {cycle_id} not found")
    if row["status"] != "awaiting_user":
        raise AssessmentError(f"assessment {cycle_id} is {row['status']}, not awaiting_user")

    now = _iso(_now())
    status = "dismissed" if decision == "dismissed" else "aligned"
    action_status = "rejected" if decision == "dismissed" else "active"
    response = {"decision": decision, "notes": notes.strip()}
    conn.execute(
        """
        UPDATE assessment_cycles
        SET status = ?, user_response = ?, aligned_at = ? WHERE id = ?
        """,
        (status, json.dumps(response), now, cycle_id),
    )
    conn.execute(
        """
        UPDATE improvement_actions SET status = ?, updated_at = ?
        WHERE assessment_id = ?
        """,
        (action_status, now, cycle_id),
    )
    conn.commit()
    result = cycle_dict(conn, cycle_id)
    _append_memory(result, response)
    store.append_event(
        conn,
        kind="assessment_aligned",
        payload={"assessment_id": cycle_id, "decision": decision},
    )
    return result


def _append_memory(cycle: dict[str, Any], response: dict[str, str]) -> None:
    memory.ensure_memory_layout()
    path = memory.improvements_path()
    lines = [
        "",
        f"## Assessment {cycle['id']} — {cycle['window_end'][:10]}",
        f"- Outcome: {response['decision']}",
        f"- Summary: {cycle.get('summary') or '(none)'}",
    ]
    if response.get("notes"):
        lines.append(f"- User alignment: {response['notes']}")
    lines.append("- Experiments:")
    for action in cycle["actions"]:
        lines.append(f"  - [{action['status']}] {action['description']}")
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def hook_context(conn: sqlite3.Connection | None = None) -> str | None:
    """Return context for a pending cycle at session start."""
    conn = conn or init_db()
    cycle = ensure_due(conn)
    if cycle is None:
        return None
    if cycle["status"] == "ready":
        return assessment_prompt(cycle)
    if cycle["status"] == "awaiting_user":
        return (
            f"Assessment {cycle['id']} is waiting for user alignment. Briefly present its "
            "performance summary and proposed actions if you have not already; inspect "
            f"`./bin/ppy assessment show {cycle['id']} --json` if needed. Then record the "
            "user's decision with `./bin/ppy assessment align`."
        )
    return None
