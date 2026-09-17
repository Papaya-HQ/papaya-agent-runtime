"""Lifecycle hook ingestion.

Provider harnesses can be configured to call ``ppy hook <event>`` at turn
boundaries (e.g. Claude Code Stop / SessionStart). The hook reads a JSON payload
on stdin, records a durable lifecycle event against the task/run, and prints a
JSON acknowledgement. This is the supervision seam for interactive managers; the
per-task runner already normalizes worker streams without hooks.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from typing import Any

from papaya_agent_runtime.state import init_db, store

LIFECYCLE_EVENTS = (
    "session_start",
    "stop",
    "session_end",
    "notification",
    "pre_tool",
    "post_tool",
)


def _normalize(event: str) -> str:
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", event.strip())
    return snake.lower().replace("-", "_")


def handle_hook(event: str, payload: dict[str, Any]) -> dict:
    """Record a lifecycle hook and return provider-compatible steering."""
    normalized = _normalize(event)
    conn = init_db()
    task_id = payload.get("task_id")
    run_id = payload.get("run_id")
    if task_id is not None and run_id is None:
        task = store.get_task(conn, int(task_id))
        run_id = task["run_id"] if task else None
    store.append_event(
        conn,
        kind=f"hook_{normalized}",
        payload={"event": normalized, **payload},
        run_id=int(run_id) if run_id is not None else None,
        task_id=int(task_id) if task_id is not None else None,
    )
    # Hook stdout is a provider-facing protocol, not an acknowledgement channel.
    # Keep the durable event metadata in the database, but emit only fields that
    # Claude Code and Codex both understand. Unknown top-level keys make both
    # harnesses report the hook result as invalid JSON output.
    result: dict[str, Any] = {}

    # Codex and Claude both accept additional context at session start and a
    # blocking continuation at Stop. The stop-active guard is supplied by the
    # harness and prevents the continuation from recursively triggering itself.
    if normalized == "session_start":
        context = session_start_context(conn, payload)
        if context:
            result["hookSpecificOutput"] = {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
    elif normalized == "stop" and not payload.get("stop_hook_active", False):
        reason = stop_block_reason(conn, payload)
        if reason:
            result.update({"decision": "block", "reason": reason})
    return result


#: The role, for a session nobody launched through `ppy start`.
#:
#: `ppy start` injects the full runtime role into the harness. A session the Papaya
#: listener started — `claude -p "<work item>"` in this directory — never goes
#: through it, and gets only `CLAUDE.md`, which is guidance a model with a job in
#: hand can reasonably deprioritise. On 2026-09-15 three such sessions did exactly
#: that: they did the work directly in other repositories and `ppy` was never
#: involved, so nothing was briefed, reviewed at an exact commit, or delivered
#: through the gate. This block is short on purpose — the contract is the long
#: form, and this is the part that has to arrive whether or not it gets read.
RUNTIME_ROLE = """YOU ARE THE PAPAYA AGENT RUNTIME (working directory: this repo).
Work here goes through `./bin/ppy`, not through editing repositories yourself:
register and onboard a repo, write a brief, `ppy dispatch` a worker into an
isolated worktree, review the exact commit, then `ppy deliver` the pull request.
If you were handed a work item, that is still how it gets built — the ledger,
the review gate and the evidence all depend on it. Read `docs/runtime-contract.md`
for anything you are unsure of, and `ppy repo list` for the only repositories you
may work on. Doing the work by hand in another checkout skips every gate this
runtime exists to provide."""


def runtime_role_context() -> str | None:
    """Tell a session it is the runtime, however it was launched.

    Skipped for framework-development sessions, which are editing this codebase
    rather than operating it.
    """
    if os.environ.get("PPY_DEV"):
        return None
    return RUNTIME_ROLE


def readiness_context() -> str | None:
    """Say, at the top of every session, when this runtime cannot actually work.

    The preflight in the contract only runs if the session reads the contract and
    chooses to. A session started NON-INTERACTIVELY — the Papaya listener running
    `claude -p "<work item>"` in this directory — arrives with a job to do and does
    it, and an unconfigured runtime stays unconfigured while work appears to happen
    somewhere else entirely. That is exactly what happened on 2026-09-15: three jobs
    ran in this working directory, none of them touched `ppy`, and nobody found out
    the runtime had never been set up.

    A hook does not depend on being read. This one fires on every session start and
    after every compaction, so the verdict is in front of the model whatever it was
    launched to do.
    """
    from papaya_agent_runtime import owed, readiness

    verdict = readiness.check()
    if verdict.state == readiness.READY:
        return None
    lines = [f"RUNTIME READINESS: {verdict.state} — {readiness.headline(verdict)}"]
    for problem in verdict.problems:
        if problem.code == owed.PROBLEM_CODE:
            continue  # the owed-work block says these, with the heartbeat, in one place
        if problem.info:
            continue  # said by the invitation, once, not as a gap
        who = "yours to fix now" if problem.owner == readiness.RUNTIME else "needs the user"
        mark = "BLOCKS WORK" if problem.blocking else "gap"
        lines.append(f"- [{mark}, {who}] {problem.summary} — {problem.fix}")
    if verdict.state == readiness.BLOCKED:
        lines.append(
            "Nothing here is a gate — you can still answer, read and help as you are. But "
            "`ppy dispatch` has nowhere to run until this is closed, so if the work in "
            "front of you needs a worker, close it first. Fix your own items as you go. "
            "For anything needing the user, say so plainly in your reply; if you are "
            "connected to Papaya, DM the connection owner what `ppy readiness --report` "
            "prints and then `ppy readiness --mark-reported`, so they hear it once."
        )
    return "\n".join(lines)


def invitation_context(payload: dict[str, Any] | None = None) -> str | None:
    """Running without Papaya: say it once, in the session's first reply, and carry on.

    Once per session: a compaction re-fires this hook, so it is skipped there, and a
    session `ppy start` launched already printed the line before the harness started.
    """
    from papaya_agent_runtime import standalone

    if os.environ.get("PPY_DEV") or (payload or {}).get("source") == "compact":
        return None
    line = standalone.invitation()
    if line is None:
        return None
    if os.environ.get("PPY_MANAGER_SESSION"):
        return (
            "RUNNING WITHOUT PAPAYA: this machine has no Papaya connection. Everything local "
            "works as usual; tickets, comments and DMs do not flow. The person was already "
            "told once at launch — do not repeat it."
        )
    return (
        "RUNNING WITHOUT PAPAYA: this machine has no Papaya connection. Everything local "
        "works as usual; tickets, comments and DMs do not flow. Do not ask the person to "
        "connect and do not block on it. Say this line once, verbatim, in your first reply "
        f"and never again this session:\n{line}"
    )


def papaya_tools_context() -> str | None:
    """Make sure a connected interactive session can act as its agent in Papaya.

    `ppy serve` turns load the Papaya MCP server themselves; a session a person opened
    here only loads its own Claude Code configuration. When this machine is connected
    and sessions here have no Papaya server (or a stale one), it is configured now, and
    the session is told how to load it without restarting. Headless turns and
    framework-development sessions are left alone.
    """
    from papaya_agent_runtime import papaya
    from papaya_agent_runtime.manager.launch import MANAGER_TURN_ENV, repo_root

    if os.environ.get("PPY_DEV") or os.environ.get(MANAGER_TURN_ENV):
        return None
    root = repo_root()
    if papaya.session_tools_ready(root):
        return None
    if papaya.status()["state"] != "connected":
        return None
    result = papaya.install_session_tools(root)
    if result["ok"]:
        return (
            f"PAPAYA TOOLS: this session acts as {result['addressed']} but started without its "
            "Papaya tools. They are configured now; ask the person to run `/mcp` (or restart "
            "the session) to load them before reading or commenting on work items. "
            "`ppy papaya tools` repeats this whenever they go missing."
        )
    return (
        "PAPAYA TOOLS: this machine is connected but this session has no Papaya tools and "
        f"configuring them failed ({result['detail']}). Run `ppy papaya tools` and say what it "
        "reports."
    )


def session_start_context(conn, payload: dict[str, Any] | None = None) -> str | None:
    """What a (re)starting manager needs to know before its first reply.

    Fires on a fresh session *and* after a compaction, so it carries the durable
    pickup context: open todos, live work, team health — plus any due assessment,
    and, first, whether this runtime can work at all.
    """
    from papaya_agent_runtime import assessments, handoff

    parts: list[str] = []
    with contextlib.suppress(Exception):  # a hook must never break the harness
        role = runtime_role_context()
        if role:
            parts.append(role)
    with contextlib.suppress(Exception):
        ready = readiness_context()
        if ready:
            parts.append(ready)
    with contextlib.suppress(Exception):
        tools = papaya_tools_context()
        if tools:
            parts.append(tools)
    with contextlib.suppress(Exception):
        invite = invitation_context(payload)
        if invite:
            parts.append(invite)
    with contextlib.suppress(Exception):
        parts.append(handoff.render_session_context(handoff.collect(conn)))
    with contextlib.suppress(Exception):
        parts.append(owed_context(conn))
    assessment = assessments.hook_context(conn)
    if assessment:
        parts.append(assessment)
    return "\n\n".join(p for p in parts if p) or None


def _headless_turn() -> bool:
    from papaya_agent_runtime.manager.launch import MANAGER_TURN_ENV

    return bool(os.environ.get(MANAGER_TURN_ENV))


def owed_context(conn) -> str | None:
    """Every worker waiting on the manager, and whether a heartbeat will hear the next one.

    An interactive session is the manager for everything no live ticket covers, so it
    starts (and restarts after a compaction) with the list and the one command that
    keeps it current. A headless `ppy serve` turn works one ticket and is not told.
    """
    from papaya_agent_runtime import owed

    if _headless_turn():
        return None
    items = owed.collect(conn)
    running = owed.running_count(conn)
    lines: list[str] = []
    mine = [item for item in items if not item.serve_owns]
    if mine:
        lines.append(f"WORKERS WAITING ON YOU ({len(mine)}) — take each up before new work:")
        lines.extend(f"- {item.line()}" for item in mine)
    if (mine or running) and not owed.watch_running():
        lines.append(
            "NO HEARTBEAT IS RUNNING. Start `./bin/ppy watch` now as a background monitor "
            "(Claude Code: the Monitor tool; Codex: a background terminal) and relay its "
            "lines. Without it a worker that finishes, stops or crashes waits unheard "
            "until someone asks."
        )
    return "\n".join(lines) or None


def owed_stop_reasons(conn) -> list[str]:
    """Why an interactive turn may not end yet: owed work with no next step, no heartbeat."""
    from papaya_agent_runtime import owed

    if _headless_turn():
        return []
    items = owed.collect(conn)
    reasons: list[str] = []
    untracked = owed.untracked(conn, items)
    if untracked:
        listed = "\n".join(f"- {item.line()}" for item in untracked)
        reasons.append(
            f"{len(untracked)} worker task(s) are waiting on you with no next step recorded "
            "against them. Take each up now, or record the next step against the task so it "
            'survives compaction: `ppy todo add --task <id> "..."`.\n' + listed
        )
    in_flight = owed.running_count(conn) + sum(1 for item in items if not item.serve_owns)
    if in_flight and not owed.watch_running():
        reasons.append(
            f"{in_flight} worker task(s) are running or waiting on you and no heartbeat is "
            "running to hear what happens next. Start `./bin/ppy watch` as a background "
            "monitor (Claude Code: the Monitor tool) before ending the turn."
        )
    return reasons


def stop_block_reason(conn, payload: dict[str, Any] | None = None) -> str | None:
    """Refuse to stop while work is open and no next step is recorded, or while the
    reply about to go out refers to things by internal label.

    These are the places a prompt-level habit becomes a hard rule. With tasks in
    flight or waiting on the manager, an empty todo ledger means "what's next" would
    be lost to the next compaction. And a reply full of "1B", "§5", "rev3" offloads
    the reader's context onto the user — the harness hands the reply back once, with
    the labels named, so it goes out described plainly. Both are skipped for
    framework-development sessions (``PPY_DEV``); the ledger gate also when nothing is
    open. All reasons found are returned together so one bounce covers everything.
    """
    from papaya_agent_runtime import assessments, board, plain_language

    assessment = assessments.hook_context(conn)
    cycle = assessments.latest_cycle(conn)
    if assessment and cycle is not None and cycle["status"] == "ready":
        return assessment
    if os.environ.get("PPY_DEV"):
        return None
    reasons: list[str] = []
    with contextlib.suppress(Exception):  # a hook must never break the harness
        reasons.extend(owed_stop_reasons(conn))
    open_tasks = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status IN "
        "('requested','in_progress','worker_done','worker_stopped','blocked',"
        "'needs_recovery','failed')"
    ).fetchone()[0]
    if open_tasks and not board.open_todos(conn):
        reasons.append(
            f"{open_tasks} task(s) are open but no next step is recorded. Before stopping, "
            'write down what happens next so it survives compaction: `ppy todo add "..."` '
            "for each next step (use `--blocked-on user|review|task:<id>` for anything "
            "waiting on someone). Then reply to the user as you were going to."
        )
    with contextlib.suppress(Exception):  # a hook must never break the harness
        plain = plain_language.stop_reason_for(payload or {})
        if plain:
            reasons.append(plain)
    return "\n\n".join(reasons) or None


def handle_hook_stdin(event: str, raw: str) -> dict:
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {"_raw": raw[:2000]}
    if not isinstance(payload, dict):
        payload = {"_raw": raw[:2000]}
    return handle_hook(event, payload)
