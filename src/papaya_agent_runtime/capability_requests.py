"""A worker asks for a capability it lacks; the runtime decides, and a person only when it must.

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
   `capabilities.auto_grant` (or a versioned variant of either) is granted at once;
   anything else is pending the manager. A person's approval joins the safe family
   (`capabilities.safe_family`), so the next similar ask is not asked.
3. **Granted** for the task (its next launch carries the pattern) or, with `--always`,
   for this install (`claude.extra_tools`); **denied** with a reason. Either way the
   worker is steered with the outcome, and a pending one is told to go on with other
   work or stop at a checkpoint.
4. **The manager decides.** A pending request is the manager's (Shane, 2026-09-18: "I,
   or anyone using the runtime should never hear about any problem that you can solve
   yourself"): `ppy serve`'s rounds hand it to the answer turn, a session sees it as the
   manager's readiness problem, and either grants or denies it with a reason.
5. **Escalated** (`ppy capability escalate <id> --why "..."`) only when it needs what
   only a person has — a credential, money, access nobody here can judge. Only then is
   it a person's blocker, which the outreach procedure says to them.

Requests live on the event log (``capability_request``, ``capability_decision``), so
there is no table to migrate and the history reads like the rest of a task's.

A request carries the pattern that makes the refused call run, which is not always
``Bash(<program>:*)`` (2026-09-22, issues #139 and #142):

- **a tool that is not the shell** (`WebFetch`, `WebSearch`, an `mcp__…` tool) is
  asked for by its own name, and its pattern is that name;
- **a program named by path** is decided as its basename (`.venv/bin/python` is
  `python` to the policy), and granted as the literal path it was run by,
  `Bash(.venv/bin/python:*)`, because `Bash(python:*)` does not match it. The family
  and ``auto_grant`` grant it only when the path resolves inside the worktree (its
  ``reach``); an absolute path, or one that leaves the worktree, waits on the manager
  with the resolved path in the request. A path is granted to its task alone, never
  to every worker: it names one worktree's files.

    state \\ event  auto_grant    never     other     approve   approve --always  deny     escalate
    (new)          auto_granted  refused   pending   —         —                 —        —
    pending        —             —         —         granted   granted (+policy) denied   escalated
    escalated      —             —         —         granted   granted (+policy) denied   —
    resolved        a repeat returns the existing request; approve/deny say it is resolved
    (task ended)    a pending or escalated request reads `moot` and asks nobody
"""

from __future__ import annotations

import contextlib
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
#: Waiting on a person: the manager escalated it, saying why only a person can decide.
ESCALATED = "escalated"
#: Pending on a task that has since ended: nobody's to answer, and nobody is asked.
MOOT = "moot"
STATES = (PENDING, GRANTED, AUTO_GRANTED, DENIED, REFUSED, ESCALATED, MOOT)
#: Not yet decided: the manager's (pending) or a person's (escalated).
OPEN = (PENDING, ESCALATED)

DECLARED = "declared"
DENIAL = "denied_command"

#: The readiness problem code for a request waiting on a person.
PROBLEM_CODE = "capability_request_pending"
#: The readiness problem code for a request the manager has not decided yet.
MANAGER_PROBLEM_CODE = "capability_request_undecided"

#: Where a program named by path resolved (`tool_learning.path_reach`). Only
#: :data:`IN_WORKTREE` may be granted by policy; the rest wait on the manager.
IN_WORKTREE = "worktree"
OUTSIDE = "outside"
ABSOLUTE = "absolute"
#: A safe-family program refused for its arguments (`find -exec`, `rm` of the worktree
#: itself): the program is safe, what it was asked to do is not, so a person decides.
ARGUMENTS = "arguments"

_PROGRAM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")
_PATTERN = re.compile(r"^Bash\(([A-Za-z0-9][A-Za-z0-9_.+-]*)(?::\*)?\)$")
#: A tool the harness has besides the shell: `WebFetch`, `mcp__server__tool`.
_TOOL = re.compile(r"^(?:mcp__[A-Za-z0-9_-]+|[A-Z][A-Za-z0-9]*)$")
#: A program path a pattern can hold literally: no spaces, globs, parens or colons.
_PATH = re.compile(r"^[A-Za-z0-9._+~/-]*/[A-Za-z0-9._+-]+$")
#: The shell is never a capability by name: `Bash` alone would allow every command.
SHELL_TOOL = "Bash"


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
    #: The program as it was run, when it was named by path (`.venv/bin/python`).
    path: str | None = None
    #: Where that path resolved: :data:`IN_WORKTREE`, :data:`OUTSIDE`, :data:`ABSOLUTE`,
    #: or :data:`ARGUMENTS` for a safe program refused for what it was asked to do.
    reach: str | None = None
    #: The path fully resolved, which is what a person deciding it needs to see.
    resolved: str | None = None

    @property
    def label(self) -> str:
        """What was asked for, as the worker ran it: the path when there was one."""
        return self.path or self.program

    def line(self) -> str:
        why = f": {self.why}" if self.why else ""
        tail = f" ({self.reason})" if self.reason else ""
        return f"request {self.id} · task {self.task_id} · `{self.label}` {self.state}{why}{tail}"

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
            "path": self.path,
            "reach": self.reach,
            "resolved": self.resolved,
        }


# ── policy ──────────────────────────────────────────────────────────────────


def floor() -> frozenset[str]:
    """Programs no policy may ever grant a worker."""
    from papaya_agent_runtime.tool_learning import POLICY

    return frozenset(POLICY)


def program_of(capability: str) -> str:
    """The program a capability names: `xcodegen` or `Bash(xcodegen:*)`. Raises when unclear.

    A tool the harness has besides the shell is named the same way (`WebSearch`,
    `mcp__server__tool`); the shell itself is not a capability.
    """
    text = capability.strip()
    match = _PATTERN.match(text)
    name = match.group(1) if match else text if _PROGRAM.match(text) else ""
    if name and name != SHELL_TOOL:
        return name
    raise CapabilityError(
        f"`{capability}` is not a program name: name one program, e.g. `xcodegen` or "
        "`Bash(xcodegen:*)`, or one tool, e.g. `WebSearch`; a path, a wildcard, the shell "
        "itself or a compound command is never granted"
    )


def is_tool(name: str) -> bool:
    """Whether ``name`` is a harness tool (`WebFetch`, `mcp__x__y`) rather than a program."""
    return name != SHELL_TOOL and bool(_TOOL.match(name))


def pattern_for(program: str, *, path: str | None = None) -> str:
    """The allowed-tools entry that lets the refused call run.

    A tool is allowed by its bare name; a program by `Bash(<prefix>:*)`, whose prefix
    the harness matches literally against the start of the command, so a program run
    by path is granted by that path.
    """
    if path:
        if not _PATH.match(path):
            raise CapabilityError(f"`{path}` cannot be written as a tool pattern")
        return f"Bash({path}:*)"
    if is_tool(program):
        return program
    return f"Bash({program}:*)"


def _policy() -> tuple[set[str], set[str]]:
    """``(auto_grant, never)`` for this install: code defaults plus config."""
    from papaya_agent_runtime import tool_learning

    policy = tool_learning.capability_policy()
    auto = set(tool_learning.safe_family(policy)) | set(policy.auto_grant)
    never = set(floor()) | set(policy.never)
    return auto - never, never


def assess(program: str) -> tuple[str, str]:
    """``(state, basis)``: what policy says about ``program``, and why when it grants.

    Grants are by intent, not only by name: beyond the safe family and `auto_grant`, a
    versioned spelling of a program this install already grants (`python3.12` beside
    `python3`) is the same request a person already answered, so it is granted without
    asking (``capabilities.intent_grants``). ``never`` and the floor still refuse first.
    """
    from papaya_agent_runtime import tool_learning

    auto, never = _policy()
    if program in never:
        return REFUSED, ""
    if program in auto:
        return AUTO_GRANTED, ""
    if tool_learning.capability_policy().intent_grants:
        # find, sed and awk are judged by name, so a spelling of one has nothing to inherit.
        base = tool_learning.variant_of(program, auto - set(tool_learning.CONDITIONAL), never)
        if base:
            return AUTO_GRANTED, f"a versioned variant of `{base}`, which this install grants"
    return PENDING, ""


def decide(program: str) -> str:
    """What this install's policy says about ``program`` before any person does."""
    return assess(program)[0]


def _generalize(approved: Request, *, by: str) -> None:
    """A person's approval joins the safe family, so its variants are learned and granted.

    Only a program the family could hold: not a tool that is not the shell, not a path
    (it names one worktree's files), not anything on the floor or `never`, and not one
    this install dropped from the family on purpose. The kind is ``run``: nothing about
    an approval says the program only reads or only writes inside the worktree.
    """
    from papaya_agent_runtime import config_changes, tool_learning
    from papaya_agent_runtime.config import load_config, save_config
    from papaya_agent_runtime.paths import config_path

    program = approved.program
    if approved.path or is_tool(program) or program in _policy()[1] or not config_path().exists():
        return
    cfg = load_config()
    caps = cfg.capabilities
    if not caps.learn_approvals or program in caps.drop_family:
        return
    if program in tool_learning.safe_family(caps):
        return
    before = dict(caps.safe_family)
    caps.safe_family = {**before, program: "run"}
    save_config(cfg)
    config_changes.record(
        key="capabilities.safe_family",
        before=before,
        after=dict(caps.safe_family),
        why=(
            f"approved `{program}` for task {approved.task_id} ({by}); its versioned "
            "variants are now granted without asking"
        ),
        evidence={"request_id": approved.id, "task_id": approved.task_id},
    )


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
                "path": payload.get("path"),
                "reach": payload.get("reach"),
                "resolved": payload.get("resolved"),
            }
        else:
            request = requests.get(int(payload.get("request_id") or 0))
            if request is not None and request["state"] in OPEN:
                request.update(
                    state=payload["state"],
                    decided_by=payload.get("by"),
                    reason=payload.get("reason"),
                    scope=payload.get("scope"),
                )
    ended = _ended_tasks(conn, {int(r["task_id"]) for r in requests.values()})
    for r in requests.values():
        if r["state"] in OPEN and int(r["task_id"]) in ended:
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
    """Requests the manager has not decided yet."""
    return [r for r in _load(conn) if r.state == PENDING]


def escalated(conn: sqlite3.Connection) -> list[Request]:
    """Requests the manager escalated: the only ones a person is asked about."""
    return [r for r in _load(conn) if r.state == ESCALATED]


def granted_patterns(conn: sqlite3.Connection, task_id: int) -> list[str]:
    """Every pattern granted on this task's requests, for its next launch.

    A path granted by policy lives here and nowhere else (it names one worktree); a
    program or tool granted by policy is also in the install's profile, and saying it
    twice costs nothing and keeps the task's launch right without a config file.
    """
    granted = (GRANTED, AUTO_GRANTED)
    return sorted({r.pattern for r in all_requests(conn, task_id=task_id) if r.state in granted})


def request(
    task_id: int,
    capability: str,
    *,
    why: str = "",
    source: str = DECLARED,
    command: str | None = None,
    path: str | None = None,
    reach: str | None = None,
    resolved: str | None = None,
) -> Request:
    """Record a need and decide what policy can. A repeat returns the existing request.

    ``capability`` names the program (for a path, its basename) or the tool. ``path``
    is the program as it was run when it was named by path, and ``reach`` where it
    resolved; anything but :data:`IN_WORKTREE` waits on the manager whatever the
    family or ``auto_grant`` would say, though ``never`` still refuses it. A repeat is
    the same pattern on the same task: another path to the same program is another
    request.

    The worker is steered when it has to hear something it did not ask to hear: every
    outcome of a request made from its denial, and a grant it declared, since a new
    tool only reaches it through the relaunch a steer starts. A declared request that
    is pending or refused is answered by the command's own output.
    """
    from papaya_agent_runtime.state import init_db, store

    program = program_of(capability)
    pattern = pattern_for(program, path=path)
    conn = init_db()
    try:
        task = store.get_task(conn, task_id)
        if task is None:
            raise CapabilityError(f"task {task_id} does not exist")
        existing = next(
            (r for r in all_requests(conn, task_id=task_id) if r.pattern == pattern), None
        )
        if existing is not None:
            return existing
        state = decide(program)
        basis = assess(program)[1] if state == AUTO_GRANTED else ""
        if state == AUTO_GRANTED and (path or reach) and reach != IN_WORKTREE:
            state = PENDING
        reason = basis or None
        if state == REFUSED:
            from papaya_agent_runtime.tool_learning import policy_rule

            reason = policy_rule(program)
        payload: dict[str, Any] = {
            "program": program,
            "pattern": pattern,
            "why": why.strip(),
            "source": source,
            "state": state,
            "command": command,
            "reason": reason,
        }
        if path or reach:
            payload.update(path=path, reach=reach, resolved=resolved)
        event_id = store.append_event(
            conn, kind=REQUEST_EVENT, payload=payload, run_id=task["run_id"], task_id=task_id
        )
        made = get(conn, int(event_id))
    finally:
        conn.close()
    assert made is not None
    if state == AUTO_GRANTED and not made.path:
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
    """The answer to an open request: the manager's, or a person's once escalated."""
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
        if found.state not in OPEN:
            raise CapabilityError(f"request {request_id} is already {found.state}")
        task = store.get_task(conn, found.task_id)
        assert task is not None
        if not approve and not reason.strip():
            raise CapabilityError("say why it is denied: the worker is told the reason")
        if approve and found.program in _policy()[1]:
            raise CapabilityError(f"`{found.program}` is never granted to a worker")
        if approve and always and found.path:
            raise CapabilityError(
                f"`{found.path}` names one worktree's files, so it is granted to task "
                f"{found.task_id} alone: approve it without --always"
            )
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
    if approve:
        # The approval stands; learning from it is a convenience.
        with contextlib.suppress(Exception):
            _generalize(decided, by=by)
    _tell_worker(decided)
    return decided


def escalate(request_id: int, *, why: str, by: str = "manager") -> Request:
    """Hand a pending request to a person, saying what only they can decide."""
    from papaya_agent_runtime.state import init_db, store

    if not why.strip():
        raise CapabilityError(
            "say what only a person can decide here (a credential, money, access nobody "
            "here can judge); anything else is yours to approve or deny"
        )
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
        store.append_event(
            conn,
            kind=DECISION_EVENT,
            payload={"request_id": request_id, "state": ESCALATED, "reason": why.strip(), "by": by},
            run_id=task["run_id"],
            task_id=found.task_id,
        )
        raised = get(conn, request_id)
    finally:
        conn.close()
    assert raised is not None
    return raised


def _record_decision(conn, decided: Request, *, always: bool, run_id: int) -> None:
    """The answer as a durable decision, so the next session does not ask again."""
    from papaya_agent_runtime.decisions import record_decision

    try:
        verb = "granted" if decided.state == GRANTED else "denied"
        record_decision(
            conn,
            question=f"May a worker run `{decided.label}`?",
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

    if granted.path or not config_path().exists():
        return  # a path is its worktree's, never every worker's
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
    name = f"`{found.label}`"
    if found.state in (GRANTED, AUTO_GRANTED):
        again = (
            "use the tool again"
            if found.pattern == found.program
            else "run the command again, exactly as you ran it, as one plain command"
        )
        return (
            f"Your request for {name} is granted as `{found.pattern}`. It is in your tool "
            f"allowlist from your next launch, which this message starts: {again}."
        )
    if found.state == REFUSED:
        return f"Your request for {name} is refused. {found.reason or ''}".strip()
    if found.state == DENIED:
        return (
            f"Your request for {name} was denied: {found.reason}. Do the work without it, and "
            'if the task cannot be done without it, record that under "Flagged, not done".'
        )
    who = "a person" if found.state == ESCALATED else "the manager"
    return (
        f"Your request for {name} is waiting on {who} (request {found.id}). Go on with "
        "any work that does not need it. If nothing is left that can be done without it, "
        "commit, report `--phase blocked` naming the request, and stop: you are resumed "
        "with the answer."
    )


def where(found: Request) -> str:
    """Why a path waits on the manager, with where it resolved; empty for anything else."""
    if not found.path and found.reach != ARGUMENTS:
        return ""
    if found.reach == ARGUMENTS:
        return " (a safe program refused for its arguments, so the family does not grant it)"
    resolved = f" resolves to `{found.resolved}`" if found.resolved else ""
    if found.reach == IN_WORKTREE:
        return f" (`{found.path}`{resolved}, inside the worktree)"
    place = "is an absolute path" if found.reach == ABSOLUTE else "leaves the worktree"
    return f" (`{found.path}`{resolved}; it {place}, so no policy grants it)"


def _tell_worker(found: Request) -> None:
    """Steer the task's worker when it is alive; a stopped one reads it on resume."""
    from papaya_agent_runtime import tool_learning

    tool_learning.steer_worker(found.task_id, worker_message(found))


def problems() -> list[Any]:
    """Open requests for readiness: the manager's to decide, or a person's once escalated."""
    from papaya_agent_runtime.paths import db_path
    from papaya_agent_runtime.readiness import RUNTIME, USER, Problem
    from papaya_agent_runtime.state import init_db

    if not db_path().exists():
        return []
    try:
        conn = init_db()
        try:
            waiting = [r for r in _load(conn) if r.state in OPEN]
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - a verdict never fails on its own evidence
        return []
    found = []
    for item in waiting:
        why = f" — {item.why}" if item.why else ""
        command = f" (denied `{item.command}`)" if item.command else ""
        why += where(item)
        if item.state == PENDING:
            found.append(
                Problem(
                    code=MANAGER_PROBLEM_CODE,
                    summary=(
                        f"worker task {item.task_id} needs `{item.label}`{why}{command}: "
                        f"request {item.id} is the manager's to decide"
                    ),
                    fix=(
                        f"`ppy capability approve {item.id}` or `ppy capability deny "
                        f'{item.id} --reason "..."`; `ppy capability escalate {item.id} '
                        '--why "..."` only for what only a person can decide'
                    ),
                    owner=RUNTIME,
                    blocking=False,
                    title=f"Decide whether a worker may run `{item.label}`",
                    scope=f"capability:{item.id}",
                )
            )
            continue
        found.append(
            Problem(
                code=PROBLEM_CODE,
                summary=(
                    f"worker task {item.task_id} needs `{item.label}`{why}{command}: "
                    f"request {item.id} is waiting on a person ({item.reason})"
                ),
                fix=(
                    f"`ppy capability approve {item.id}` (add `--always` for every worker on "
                    f'this machine) or `ppy capability deny {item.id} --reason "..."`'
                ),
                owner=USER,
                blocking=False,
                title=f"A worker needs `{item.label}`",
                steps=(
                    f"worker task {item.task_id} asked to run `{item.label}`{why}",
                    f"only you can decide it because: {item.reason}",
                    f"to allow it for this task: ppy capability approve {item.id}",
                    f"for every worker on this machine: ppy capability approve {item.id} --always",
                    f'to refuse: ppy capability deny {item.id} --reason "<why>"',
                    "the worker is told the answer and resumed with it",
                ),
                scope=f"capability:{item.id}",
            )
        )
    return found
