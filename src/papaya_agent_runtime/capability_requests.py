"""A worker asks for a capability it lacks; the runtime grants it or asks a person.

Every install has different permissions. Until 2026-09-17 a worker found out what it
could not run by trying: task 11 lost fifteen minutes to `xcodegen` denials and then
hand-edited a generated Xcode project; task 7 learned its browser capture was refused
only after the code was done; a `psql` denial sat in readiness with nobody deciding.
The runtime learned only the closed safe family (:mod:`tool_learning`) and said
everything else once, as a gap.

Now there is one loop, the same whether the worker declared the need or was denied:

1. **Declared.** `ppy need <task> --capability <program> --why "..."` in the plan phase,
   or a plain command refused for a gap in the profile, becomes a request on the task.
2. **Decided** against this install's policy: a program on the floor (:func:`floor`,
   plus `capabilities.never`) is refused with the rule; one in the safe family or in
   `capabilities.auto_grant` is granted at once; anything else is pending a person.
3. **Granted** for the task (its next launch carries the pattern) or, with `--always`,
   for this install (`claude.extra_tools`); **denied** with a reason. Either way the
   worker is steered with the outcome, and a pending one is told to go on with other
   work or stop at a checkpoint.
4. **Escalated.** A pending request is a person's blocker (:func:`problems`), which
   readiness raises, `ppy serve` reports, and an interactive session sees at start.

Requests live on the event log (``capability_request``, ``capability_decision``), so
there is no table to migrate and the history reads like the rest of a task's.

    state \\ event   auto_grant     never      other      approve      approve --always   deny
    (new)           auto_granted   refused    pending    —            —                  —
    pending         —              —          —          granted      granted (+policy)  denied
    resolved        a repeat returns the existing request; approve/deny say it is resolved
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from papaya_agent_runtime.lifecycle import TERMINAL_STATUSES

REQUEST_EVENT = "capability_request"
DECISION_EVENT = "capability_decision"

PENDING = "pending"
GRANTED = "granted"
AUTO_GRANTED = "auto_granted"
DENIED = "denied"
REFUSED = "refused"
#: Pending on a task that has since ended: nobody's to answer, and nobody is asked.
MOOT = "moot"
STATES = (PENDING, GRANTED, AUTO_GRANTED, DENIED, REFUSED, MOOT)

DECLARED = "declared"
DENIAL = "denied_command"

#: The readiness problem code for a request waiting on a person.
PROBLEM_CODE = "capability_request_pending"

_PROGRAM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_PATTERN = re.compile(r"^Bash\(([A-Za-z0-9][A-Za-z0-9._+-]*)(?::\*)?\)$")


class CapabilityError(RuntimeError):
    """A request or a decision that cannot be made as asked."""


@dataclass(frozen=True)
class Request:
    id: int
    task_id: int
    program: str
    pattern: str
    why: str
    source: str
    state: str
    command: str | None = None
    decided_by: str | None = None
    reason: str | None = None
    scope: str | None = None

    def line(self) -> str:
        why = f": {self.why}" if self.why else ""
        tail = f" ({self.reason})" if self.reason else ""
        return f"request {self.id} · task {self.task_id} · `{self.program}` {self.state}{why}{tail}"

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "program": self.program,
            "pattern": self.pattern,
            "why": self.why,
            "source": self.source,
            "state": self.state,
            "command": self.command,
            "decided_by": self.decided_by,
            "reason": self.reason,
            "scope": self.scope,
        }


# ── policy ──────────────────────────────────────────────────────────────────


def floor() -> frozenset[str]:
    """Programs no policy may ever grant a worker."""
    from papaya_agent_runtime.tool_learning import POLICY

    return frozenset(POLICY)


def program_of(capability: str) -> str:
    """The program a capability names: `xcodegen` or `Bash(xcodegen:*)`. Raises when unclear."""
    text = capability.strip()
    match = _PATTERN.match(text)
    if match:
        return match.group(1)
    if _PROGRAM.match(text):
        return text
    raise CapabilityError(
        f"`{capability}` is not a program name: name one program, e.g. `xcodegen` or "
        "`Bash(xcodegen:*)`; a path, a wildcard or a compound command is never granted"
    )


def pattern_for(program: str) -> str:
    return f"Bash({program}:*)"


def _policy() -> tuple[set[str], set[str]]:
    """``(auto_grant, never)`` for this install: code defaults plus config."""
    from papaya_agent_runtime.tool_learning import SAFE_FAMILY

    auto, never = set(SAFE_FAMILY), set(floor())
    try:
        from papaya_agent_runtime.config import load_config
        from papaya_agent_runtime.paths import config_path

        if config_path().exists():
            cfg = load_config()
            auto |= set(cfg.capabilities.auto_grant)
            never |= set(cfg.capabilities.never)
    except Exception:  # noqa: BLE001 - an unreadable config falls back to the code's policy
        pass
    return auto - never, never


def decide(program: str) -> str:
    """What this install's policy says about ``program`` before any person does."""
    auto, never = _policy()
    if program in never:
        return REFUSED
    if program in auto:
        return AUTO_GRANTED
    return PENDING


# ── the record ──────────────────────────────────────────────────────────────


def _load(conn: sqlite3.Connection, where: str = "", params: tuple = ()) -> list[Request]:
    rows = conn.execute(
        f"SELECT id, task_id, kind, payload FROM events WHERE kind IN (?, ?) {where} ORDER BY id",
        (REQUEST_EVENT, DECISION_EVENT, *params),
    ).fetchall()
    requests: dict[int, dict[str, Any]] = {}
    for row in rows:
        payload = json.loads(row["payload"])
        if row["kind"] == REQUEST_EVENT:
            requests[int(row["id"])] = {
                "id": int(row["id"]),
                "task_id": int(row["task_id"]),
                "program": payload["program"],
                "pattern": payload["pattern"],
                "why": payload.get("why") or "",
                "source": payload.get("source") or DECLARED,
                "state": payload.get("state") or PENDING,
                "command": payload.get("command"),
                "reason": payload.get("reason"),
            }
        else:
            request = requests.get(int(payload.get("request_id") or 0))
            if request is not None and request["state"] == PENDING:
                request.update(
                    state=payload["state"],
                    decided_by=payload.get("by"),
                    reason=payload.get("reason"),
                    scope=payload.get("scope"),
                )
    ended = _ended_tasks(conn, {int(r["task_id"]) for r in requests.values()})
    for r in requests.values():
        if r["state"] == PENDING and int(r["task_id"]) in ended:
            r["state"] = MOOT
    return [Request(**r) for r in requests.values()]


def _ended_tasks(conn: sqlite3.Connection, task_ids: set[int]) -> set[int]:
    """The tasks among ``task_ids`` that are over or gone: a request on one reaches nobody."""
    if not task_ids:
        return set()
    marks = ",".join("?" for _ in task_ids)
    live = {
        int(row[0]): str(row[1])
        for row in conn.execute(
            f"SELECT id, status FROM tasks WHERE id IN ({marks})", tuple(task_ids)
        ).fetchall()
    }
    return {t for t in task_ids if live.get(t) is None or live[t] in TERMINAL_STATUSES}


def all_requests(conn: sqlite3.Connection, *, task_id: int | None = None) -> list[Request]:
    found = _load(conn)
    return [r for r in found if task_id is None or r.task_id == task_id]


def get(conn: sqlite3.Connection, request_id: int) -> Request | None:
    return next((r for r in _load(conn) if r.id == request_id), None)


def pending(conn: sqlite3.Connection) -> list[Request]:
    return [r for r in _load(conn) if r.state == PENDING]


def granted_patterns(conn: sqlite3.Connection, task_id: int) -> list[str]:
    """The patterns granted to this task alone, for its next launch."""
    return sorted({r.pattern for r in all_requests(conn, task_id=task_id) if r.state == GRANTED})


def request(
    task_id: int,
    capability: str,
    *,
    why: str = "",
    source: str = DECLARED,
    command: str | None = None,
) -> Request:
    """Record a need and decide what policy can. A repeat returns the existing request.

    The worker is steered when it has to hear something it did not ask to hear: every
    outcome of a request made from its denial, and a grant it declared, since a new
    tool only reaches it through the relaunch a steer starts. A declared request that
    is pending or refused is answered by the command's own output.
    """
    from papaya_agent_runtime.state import init_db, store

    program = program_of(capability)
    conn = init_db()
    try:
        task = store.get_task(conn, task_id)
        if task is None:
            raise CapabilityError(f"task {task_id} does not exist")
        existing = next(
            (r for r in all_requests(conn, task_id=task_id) if r.program == program), None
        )
        if existing is not None:
            return existing
        state = decide(program)
        reason = None
        if state == REFUSED:
            from papaya_agent_runtime.tool_learning import policy_rule

            reason = policy_rule(program)
        event_id = store.append_event(
            conn,
            kind=REQUEST_EVENT,
            payload={
                "program": program,
                "pattern": pattern_for(program),
                "why": why.strip(),
                "source": source,
                "state": state,
                "command": command,
                "reason": reason,
            },
            run_id=task["run_id"],
            task_id=task_id,
        )
        made = get(conn, int(event_id))
    finally:
        conn.close()
    assert made is not None
    if state == AUTO_GRANTED:
        _grant_for_install(made, by="policy")
    if source != DECLARED or state == AUTO_GRANTED:
        _tell_worker(made)
    return made


def decide_request(
    request_id: int,
    *,
    approve: bool,
    always: bool = False,
    reason: str = "",
    by: str = "person",
) -> Request:
    """A person's answer to a pending request."""
    from papaya_agent_runtime.state import init_db, store

    conn = init_db()
    try:
        found = get(conn, request_id)
        if found is None:
            raise CapabilityError(f"there is no capability request {request_id}")
        if found.state == MOOT:
            raise CapabilityError(
                f"task {found.task_id} has ended; request {request_id} is moot and needs no answer"
            )
        if found.state != PENDING:
            raise CapabilityError(f"request {request_id} is already {found.state}")
        task = store.get_task(conn, found.task_id)
        assert task is not None
        if not approve and not reason.strip():
            raise CapabilityError("say why it is denied: the worker is told the reason")
        if approve and found.program in _policy()[1]:
            raise CapabilityError(f"`{found.program}` is never granted to a worker")
        store.append_event(
            conn,
            kind=DECISION_EVENT,
            payload={
                "request_id": request_id,
                "state": GRANTED if approve else DENIED,
                "scope": ("install" if always else "task") if approve else None,
                "reason": reason.strip() or None,
                "by": by,
            },
            run_id=task["run_id"],
            task_id=found.task_id,
        )
        decided = get(conn, request_id)
        assert decided is not None
        _record_decision(conn, decided, always=always, run_id=task["run_id"])
    finally:
        conn.close()
    if approve and always:
        _grant_for_install(decided, by=by)
    _tell_worker(decided)
    return decided


def _record_decision(conn, decided: Request, *, always: bool, run_id: int) -> None:
    """The answer as a durable decision, so the next session does not ask again."""
    from papaya_agent_runtime.decisions import record_decision

    try:
        verb = "granted" if decided.state == GRANTED else "denied"
        record_decision(
            conn,
            question=f"May a worker run `{decided.program}`?",
            answer=verb + (f": {decided.reason}" if decided.reason else ""),
            scope="global" if always else "task",
            run_id=run_id,
            task_id=decided.task_id,
            rationale=decided.why or None,
            author=decided.decided_by or "user",
        )
    except Exception:  # noqa: BLE001 - the event is the record; the decision is a convenience
        pass


def _grant_for_install(granted: Request, *, by: str) -> None:
    """Add the pattern to this install's worker profile, recorded as a config change."""
    from papaya_agent_runtime import config_changes
    from papaya_agent_runtime.config import effective_claude_tools, load_config, save_config
    from papaya_agent_runtime.paths import config_path

    if not config_path().exists():
        return
    cfg = load_config()
    if granted.pattern in effective_claude_tools(cfg):
        return
    before = list(cfg.claude.extra_tools)
    if granted.pattern in cfg.claude.dropped_tools:
        cfg.claude.dropped_tools = [t for t in cfg.claude.dropped_tools if t != granted.pattern]
    else:
        cfg.claude.extra_tools = [*before, granted.pattern]
    save_config(cfg)
    config_changes.record(
        key="claude.extra_tools",
        before=before,
        after=list(cfg.claude.extra_tools),
        why=(
            f"granted {granted.pattern} to every worker ({by}): task {granted.task_id} "
            f"asked for `{granted.program}`" + (f" — {granted.why}" if granted.why else "")
        ),
        evidence={"request_id": granted.id, "task_id": granted.task_id},
    )


# ── the worker, and the person ──────────────────────────────────────────────


def worker_message(found: Request) -> str:
    name = f"`{found.program}`"
    if found.state in (GRANTED, AUTO_GRANTED):
        return (
            f"Your request for {name} is granted. It is in your tool allowlist from your next "
            "launch, which this message starts: run the command again, as one plain command."
        )
    if found.state == REFUSED:
        return f"Your request for {name} is refused. {found.reason or ''}".strip()
    if found.state == DENIED:
        return (
            f"Your request for {name} was denied: {found.reason}. Do the work without it, and "
            'if the task cannot be done without it, record that under "Flagged, not done".'
        )
    return (
        f"Your request for {name} is waiting on a person (request {found.id}). Go on with "
        "any work that does not need it. If nothing is left that can be done without it, "
        "commit, report `--phase blocked` naming the request, and stop: you are resumed "
        "with the answer."
    )


def _tell_worker(found: Request) -> None:
    """Steer the task's worker when it is alive; a stopped one reads it on resume."""
    from papaya_agent_runtime import tool_learning

    tool_learning.steer_worker(found.task_id, worker_message(found))


def problems() -> list[Any]:
    """Each pending request as a person's non-blocking problem, for readiness."""
    from papaya_agent_runtime.paths import db_path
    from papaya_agent_runtime.readiness import USER, Problem
    from papaya_agent_runtime.state import init_db

    if not db_path().exists():
        return []
    try:
        conn = init_db()
        try:
            waiting = pending(conn)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - a verdict never fails on its own evidence
        return []
    found = []
    for item in waiting:
        why = f" — {item.why}" if item.why else ""
        command = f" (denied `{item.command}`)" if item.command else ""
        found.append(
            Problem(
                code=PROBLEM_CODE,
                summary=(
                    f"worker task {item.task_id} needs `{item.program}`{why}{command}: "
                    f"request {item.id} is waiting on a person"
                ),
                fix=(
                    f"`ppy capability approve {item.id}` (add `--always` for every worker on "
                    f'this machine) or `ppy capability deny {item.id} --reason "..."`'
                ),
                owner=USER,
                blocking=False,
                title=f"A worker needs `{item.program}`",
                steps=(
                    f"worker task {item.task_id} asked to run `{item.program}`{why}",
                    f"to allow it for this task: ppy capability approve {item.id}",
                    f"for every worker on this machine: ppy capability approve {item.id} --always",
                    f'to refuse: ppy capability deny {item.id} --reason "<why>"',
                    "the worker is told the answer and resumed with it",
                ),
                scope=f"capability:{item.id}",
            )
        )
    return found
