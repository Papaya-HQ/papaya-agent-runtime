"""Session handoff: a pickup prompt that resumes the manager from durable state.

The manager's working context is the one thing Papaya Agent Runtime cannot make durable by
itself — compaction, a closed terminal, or a fresh session drop whatever only lived
in the conversation. The rule is simple: **record state first, then hand off.** The
todo ledger (``ppy todo``) carries the manager's intent; ``.ppy/state.db`` carries the
team's lifecycle; memory carries the learnings. ``ppy handoff`` reads all three, checks
on the team (supervisor reachable? every in-flight worker alive and talking?), writes
the resulting **snapshot** — open work, ledger, known risks — to
``.ppy/memory/handoff.md`` (a generated projection, like the board), and prints a short
**pickup prompt** the user pastes into the next session. The prompt does not carry the
snapshot; it points at the file, so the thing the user pastes stays a few lines long
no matter how much is in flight. The same collection feeds the SessionStart hook, so a
session that restarts after compaction gets the pickup context injected automatically.

Judgment — what to record before handing off, and how to speak to the user — lives in
the ``handoff`` skill. This module owns only the mechanics and is deliberately
independent of the supervisor daemon: a handoff must work even when nothing else is
running (that is one of the things it warns about).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from papaya_agent_runtime import board, health, memory, progress
from papaya_agent_runtime.state import init_db, store

IN_FLIGHT = ("requested", "in_progress")
NEEDS_ME = ("worker_done", "worker_stopped", "blocked", "needs_recovery", "failed")
DONE = ("delivered",)

_STATUS_HINT = {
    "requested": "queued, not started",
    "in_progress": "worker running",
    "worker_done": "finished — review the diff, then deliver",
    "worker_stopped": "turn ended mid-gate — `ppy resume` it; the reason is on the task",
    "blocked": "waiting on an answer",
    "needs_recovery": "runner dropped — `ppy reconcile`, then `ppy resume`",
    "failed": "failed — inspect events; rework or re-dispatch",
    "delivered": "delivered",
}


# --------------------------------------------------------------------------- #
# Collect
# --------------------------------------------------------------------------- #


def _open_questions(conn: sqlite3.Connection, run_id: int) -> dict[int, str]:
    """Latest human-facing question per blocked task in a run."""
    out: dict[int, str] = {}
    for ev in store.actionable_events(conn, run_id):
        if ev["kind"] not in ("question", "blocked") or ev["task_id"] is None:
            continue
        try:
            payload = json.loads(ev["payload"])
        except (TypeError, ValueError):
            payload = {}
        text = payload.get("question") or payload.get("summary")
        if text:
            out[int(ev["task_id"])] = str(text)
    return out


def _latest_review(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM reviews WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,)
    ).fetchone()


def _task_entry(
    conn: sqlite3.Connection, row: sqlite3.Row, repos: dict[int, str], questions: dict[int, str]
) -> dict[str, Any]:
    status = row["status"]
    entry: dict[str, Any] = {
        "id": row["id"],
        "title": row["title"],
        "repo": repos.get(row["repo_id"]) if row["repo_id"] is not None else None,
        "provider": row["provider"],
        "status": status,
        "hint": _STATUS_HINT.get(status, status),
    }
    latest = progress.latest(row["id"], conn=conn)
    if latest is not None:
        entry["progress"] = {"phase": latest["phase"], "note": latest["note"], "at": latest["at"]}
    if status == "blocked" and row["id"] in questions:
        entry["question"] = questions[row["id"]]
    if status == "worker_done":
        review = _latest_review(conn, row["id"])
        if review is None:
            entry["review"] = "not reviewed"
        elif review["verdict"] == "approved":
            entry["review"] = f"approved at {review['head_sha'][:8]} — deliver if head unchanged"
        else:
            entry["review"] = f"{review['verdict']} at {review['head_sha'][:8]}"
    return entry


def supervisor_status() -> dict[str, Any]:
    """Is the background supervisor reachable? Quick, and never raises."""
    from papaya_agent_runtime.supervisor.client import SupervisorClient, SupervisorUnavailable

    client = SupervisorClient()
    try:
        resp = client._call({"cmd": "ping"}, timeout=3.0)
    except SupervisorUnavailable:
        return {"reachable": False, "socket": client.socket_path}
    except OSError:
        return {"reachable": False, "socket": client.socket_path, "hung": True}
    return {"reachable": True, "pid": resp.get("pid"), "socket": client.socket_path}


def collect(conn: sqlite3.Connection | None = None, *, recent_runs: int = 10) -> dict[str, Any]:
    """Gather everything a fresh session needs to resume, from durable state only."""
    conn = conn or init_db()
    repos = {r["id"]: r["name"] for r in store.list_repos(conn)}
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY updated_at DESC, id DESC LIMIT ?", (recent_runs,)
    ).fetchall()

    open_runs: list[dict[str, Any]] = []
    for run in rows:
        tasks = store.list_tasks(conn, run["id"])
        questions = _open_questions(conn, run["id"])
        entries = [_task_entry(conn, t, repos, questions) for t in tasks]
        in_flight = [e for e in entries if e["status"] in IN_FLIGHT]
        needs_me = [e for e in entries if e["status"] in NEEDS_ME]
        if not (in_flight or needs_me):
            continue
        open_runs.append(
            {
                "id": run["id"],
                "objective": run["objective"],
                "in_flight": in_flight,
                "needs_me": needs_me,
                "delivered": sum(1 for e in entries if e["status"] in DONE),
                "task_count": len(entries),
            }
        )

    assessment = None
    from papaya_agent_runtime import assessments

    cycle = assessments.latest_cycle(conn)
    if cycle is not None and cycle["status"] in assessments.OPEN_STATUSES:
        assessment = {"id": cycle["id"], "status": cycle["status"]}

    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "open_runs": open_runs,
        "assessment": assessment,
        "next": board.next_steps(conn),
        "waiting": board.waiting(conn),
        "supervisor": supervisor_status(),
        "health": health.check(conn, quiet_after=health.quiet_threshold()),
    }


# --------------------------------------------------------------------------- #
# Warnings — what could go wrong for the team while the manager is away
# --------------------------------------------------------------------------- #


def warnings_for(data: dict[str, Any]) -> list[str]:
    out: list[str] = []
    in_flight = [e for r in data["open_runs"] for e in r["in_flight"]]
    by_verdict: dict[str, list[dict[str, Any]]] = {"alive": [], "quiet": [], "dead": []}
    for e in data["health"]:
        by_verdict.setdefault(e["verdict"], []).append(e)

    sup = data["supervisor"]
    if in_flight and not sup["reachable"]:
        ids = ", ".join(str(e["id"]) for e in in_flight)
        why = "hung" if sup.get("hung") else "not running"
        out.append(
            f"Supervisor is {why}: task(s) {ids} are NOT making progress and nothing will "
            "pick up their results. On resume, start it (`ppy supervisor serve` in the "
            "background), then `ppy reconcile`."
        )
    if by_verdict["dead"]:
        ids = ", ".join(str(e["task_id"]) for e in by_verdict["dead"])
        out.append(
            f"Worker process gone for task(s) {ids} with no result recorded. "
            "On resume: `ppy reconcile` (marks needs_recovery), then `ppy resume <id>`."
        )
    for e in by_verdict["quiet"]:
        out.append(
            f'Task {e["task_id"]} "{e["title"]}" has been silent for '
            f"{health.humanize(e['silent_seconds'])} — it may be stuck. On resume, check "
            "`ppy task <id>` and steer/resume it, or `ppy reconcile` if it's wedged."
        )
    if by_verdict["alive"] and sup["reachable"]:
        ids = ", ".join(str(e["task_id"]) for e in by_verdict["alive"])
        out.append(
            f"{len(by_verdict['alive'])} worker(s) still running (task {ids}). They keep "
            "going in the background; finished work and questions queue in state until "
            "you're back — a worker that asks a question will sit idle until it's "
            "answered. If this terminal or the machine goes down, they stop with it: "
            "on resume run `ppy reconcile` and `ppy health` before trusting any status."
        )
    for e in in_flight:
        if e["status"] == "in_progress" and not e.get("progress"):
            out.append(
                f'Task {e["id"]} "{e["title"]}" has not posted a plan — you have not seen '
                "its approach yet."
            )
    for run in data["open_runs"]:
        for e in run["needs_me"]:
            if e["status"] == "blocked":
                q = f': "{e["question"]}"' if e.get("question") else ""
                out.append(f'Task {e["id"]} "{e["title"]}" is idle waiting on an answer{q}.')
            elif e["status"] == "worker_done" and e.get("review") == "not reviewed":
                out.append(f'Task {e["id"]} "{e["title"]}" is finished and waiting on review.')
            elif e["status"] in ("needs_recovery", "failed"):
                out.append(f'Task {e["id"]} "{e["title"]}" is {e["status"]} — {e["hint"]}.')
    if data["open_runs"] and not (data["next"] or data["waiting"]):
        out.append(
            'No next step is recorded while runs are open — `ppy todo add "..."` what '
            "happens next (and `--blocked-on user|review|task:<id>` for anything waiting), "
            "or it's lost."
        )
    return out


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #


def _task_brief(e: dict[str, Any]) -> str:
    where = f" ({e['repo']})" if e.get("repo") else ""
    s = f'task {e["id"]} "{e["title"]}"{where} — {e["status"]}'
    if e.get("progress"):
        s += f" · phase {e['progress']['phase']}"
        if e["progress"].get("note"):
            s += f' ("{e["progress"]["note"]}")'
    if e.get("question"):
        s += f' · question: "{e["question"]}"'
    if e.get("review") and e["review"] != "not reviewed":
        s += f" · review {e['review']}"
    return s


def _todo_brief(t: dict[str, Any]) -> str:
    s = f"#{t['id']} {t['text']}"
    if t.get("blocked_on"):
        s += f" (waiting on {t['blocked_on']})"
    return s


def _snapshot_lines(data: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if data["next"]:
        lines.append("- Next (todo ledger): " + "; ".join(_todo_brief(t) for t in data["next"]))
    if data["waiting"]:
        lines.append(
            "- Waiting (todo ledger): " + "; ".join(_todo_brief(t) for t in data["waiting"])
        )
    for run in data["open_runs"]:
        parts = [_task_brief(e) for e in (*run["needs_me"], *run["in_flight"])]
        lines.append(f'- Run {run["id"]} "{run["objective"]}": ' + "; ".join(parts))
    if not data["open_runs"]:
        lines.append("- Nothing in flight; nothing waiting on me.")
    if data["assessment"]:
        a = data["assessment"]
        lines.append(
            f"- Assessment cycle {a['id']} is {a['status']} — handle via `ppy assessment`."
        )
    return lines


def render_snapshot(data: dict[str, Any], warnings: list[str]) -> str:
    """The snapshot file (`.ppy/memory/handoff.md`): everything the prompt used to carry."""
    lines = [
        "# Handoff snapshot",
        "",
        f"Generated by `ppy handoff` at {data['generated_at']} from `.ppy/state.db` and the "
        "todo ledger — do not hand-edit; rerun `ppy handoff` to refresh. Live state wins: "
        "reconcile with `ppy status`, `ppy run <id>`, `ppy task <id>`, `ppy health` before acting.",
        "",
        "## Snapshot",
        *_snapshot_lines(data),
    ]
    if warnings:
        lines += ["", "## Known risks at handoff (verify each on resume)"]
        lines.extend(f"- {w}" for w in warnings)
    return "\n".join(lines) + "\n"


def snapshot_path() -> Path:
    return memory.handoff_path()


def _display_path(path: Path) -> str:
    """Show the snapshot path relative to the working directory when it lives inside it."""
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def render_pickup_prompt(
    data: dict[str, Any], warnings: list[str], snapshot: Path | None = None
) -> str:
    """The prompt the user pastes to resume. Short; the snapshot lives in the file it names."""
    path = _display_path(snapshot or snapshot_path())
    open_runs = len(data["open_runs"])
    ledger = len(data["next"]) + len(data["waiting"])
    summary = (
        f"{open_runs} open run(s), {ledger} ledger item(s), {len(warnings)} known risk(s)"
        if (open_runs or ledger or warnings)
        else "nothing in flight; nothing waiting on me"
    )
    lines: list[str] = [
        "Pick up where we left off. You're Papaya Agent Runtime at runtime (follow "
        "docs/runtime-contract.md; run preflight silently). Do all of this BEFORE you say "
        "anything to me:",
        "1. Reconnect to the team: `./bin/ppy supervisor status` — if it's down, start it "
        "in the background (`./bin/ppy supervisor serve`) — then `./bin/ppy reconcile` and "
        "`./bin/ppy health` to find dead or stuck workers.",
        f"2. Read the handoff snapshot at `{path}` (as of {data['generated_at']}: "
        f"{summary}) — it holds the ledger, every open run and task, and the known risks "
        "at handoff to verify on resume.",
        "3. Load your ledger and memory: `./bin/ppy todo list` and `./bin/ppy board`; then "
        "`.ppy/memory/preferences.md`, `relationships.md`, `improvements.md`, and "
        "`repos/<name>/notes.md` for any repo in the snapshot.",
        "4. Reconcile the snapshot against live state (`./bin/ppy run <id>` per open run, "
        "`./bin/ppy task <id>` for each worker's latest progress) — workers keep working "
        "between sessions, so live state wins. Don't re-ask anything already on file.",
    ]
    if data["assessment"]:
        a = data["assessment"]
        lines.append(f"Assessment cycle {a['id']} is {a['status']} — handle via `ppy assessment`.")
    lines.append("")
    lines.append(
        "Then greet me with one dry line: where things stand, anything that went wrong "
        "while I was gone, and the single most actionable thing right now."
    )
    return "\n".join(lines)


def render_session_context(data: dict[str, Any]) -> str | None:
    """Compact pickup context for the SessionStart hook (fresh session or post-compaction)."""
    if not (data["open_runs"] or data["next"] or data["waiting"]):
        return None
    lines = [
        "Papaya Agent Runtime pickup context (from `ppy` durable state; live state wins — "
        "reconcile with `ppy status`, `ppy run <id>`, `ppy health` before acting):",
        *_snapshot_lines(data),
    ]
    warnings = warnings_for(data)
    if warnings:
        lines.append("Attention:")
        lines.extend(f"- {w}" for w in warnings)
    return "\n".join(lines)


def write_snapshot(data: dict[str, Any], warnings: list[str]) -> Path:
    """Project the snapshot to ``.ppy/memory/handoff.md`` and return its path."""
    memory.ensure_memory_layout()
    path = snapshot_path()
    path.write_text(render_snapshot(data, warnings))
    return path


def build_handoff(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Collect, warn, write the snapshot, render the prompt that names it.

    Returns ``{prompt, warnings, snapshot, data}``. The snapshot file is the only thing
    written; the ledger, state, and memory were recorded before this was called.
    """
    data = collect(conn)
    warnings = warnings_for(data)
    path = write_snapshot(data, warnings)
    return {
        "prompt": render_pickup_prompt(data, warnings, path),
        "warnings": warnings,
        "snapshot": str(path),
        "data": data,
    }
