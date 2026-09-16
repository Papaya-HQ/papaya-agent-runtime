"""Learn the Claude worker's tools from what workers are actually denied.

PAP-219's worker was refused `python3 -c`, a `cp`, and every compound command, and the
runtime watched it happen: the denials were in the worker's own result event and
nothing read them. Now every denial is a `permission_denied` event, and a denied
command in the **safe family** below adds its pattern to `claude.extra_tools` (or
restores it from `claude.dropped_tools`), recorded as a `config_change` with the
denial as evidence, so the next dispatch has it.

The safe family is a closed list in code, and the only thing ever added is
`Bash(<program>:*)` for a program on it. Nothing else is ever learned: no `Bash(*)`,
no `sudo`, no network tool, no command named by an arbitrary path, no compound or
redirected command, and no file verb whose target is outside the worker's worktree.
A denial outside the family changes nothing; readiness surfaces it once, with the
exact pattern a person would add.

A learned pattern is a prefix, so once `Bash(cp:*)` is learned from a `cp` inside the
worktree it matches any `cp`. That is the same trade the documented profile already
makes: containment is the worker's write boundary, not this verb list.
"""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass

from papaya_agent_runtime.config import PPY_LAUNCHER_PATTERN

#: A worker's refused tool call.
PERMISSION_DENIED = "permission_denied"

#: Programs that only read, whatever their arguments.
READ_ONLY = (
    "cat",
    "cut",
    "diff",
    "file",
    "grep",
    "head",
    "jq",
    "ls",
    "rg",
    "sort",
    "stat",
    "tail",
    "tr",
    "uniq",
    "wc",
)
#: Toolchains and interpreters a worker runs its own repository's code with.
TOOLCHAINS = (
    "cargo",
    "corepack",
    "go",
    "make",
    "node",
    "npm",
    "npx",
    "pnpm",
    "pytest",
    "python",
    "python3",
    "ruff",
    "uv",
)
#: File verbs, learned only from a call whose every path is inside the worktree.
WORKTREE_WRITES = ("cp", "mkdir", "mv", "rm", "tee", "touch")
#: Read-only unless told otherwise; the checks in :func:`classify` refuse the rest.
CONDITIONAL = ("awk", "find", "sed")

#: The safe family: program -> kind.
SAFE_FAMILY: dict[str, str] = {
    **dict.fromkeys(READ_ONLY, "read"),
    **dict.fromkeys(TOOLCHAINS, "run"),
    **dict.fromkeys(WORKTREE_WRITES, "write"),
    **dict.fromkeys(CONDITIONAL, "conditional"),
}

#: Never learned, and a test holds the family to it.
NEVER = frozenset(
    {
        "bash",
        "chmod",
        "chown",
        "curl",
        "dd",
        "env",
        "eval",
        "exec",
        "gh",
        "nc",
        "open",
        "osascript",
        "rsync",
        "scp",
        "sh",
        "ssh",
        "sudo",
        "wget",
        "xargs",
        "zsh",
    }
)

_FIND_ACTIONS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fls"})
_OPERATORS = set(";&|<>`\n")


@dataclass(frozen=True)
class Verdict:
    """What a denied tool call means for the profile."""

    #: The pattern that would have allowed it (learned when ``in_family``).
    pattern: str
    in_family: bool
    reason: str


def _unquoted(command: str) -> str | None:
    """The command with quoted text removed; None when quoting is unbalanced."""
    out: list[str] = []
    quote = ""
    escaped = False
    for ch in command:
        if escaped:
            escaped = False
            continue
        if ch == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
            continue
        out.append(ch)
    return None if quote else "".join(out)


def _inside(arg: str, worktree: str, *, strict: bool = False) -> bool:
    if arg.startswith("~") or "$" in arg:
        return False
    root = os.path.normpath(worktree)
    path = os.path.normpath(arg if os.path.isabs(arg) else os.path.join(root, arg))
    if path == root:
        return not strict
    return path.startswith(root + os.sep)


def _suggest(program: str) -> str:
    return f"Bash({program}:*)"


def classify(tool: str, command: str | None, worktree: str | None) -> Verdict:
    """Is this denial in the safe family, and which pattern would have allowed it?"""
    if tool != "Bash":
        return Verdict(tool, False, f"{tool} is not a shell command; only Bash is learned")
    command = (command or "").strip()
    bare = _unquoted(command)
    if not command or bare is None:
        return Verdict("", False, "the command could not be read")
    try:
        words = shlex.split(command)
    except ValueError:
        return Verdict("", False, "the command could not be read")
    program = words[0] if words else ""
    suggestion = _suggest(program)
    if _OPERATORS & set(bare) or "$(" in bare:
        return Verdict(
            suggestion,
            False,
            "a compound, piped or redirected command is refused by the command rules; "
            "split it into one command per call",
        )
    if "=" in program and not program.startswith(("/", ".")):
        return Verdict("", False, "an inline environment assignment is never learned")
    if program == "ppy" or program == "./bin/ppy":
        return Verdict(suggestion, True, "the runtime's own launcher")
    if program.endswith("/bin/ppy"):
        return Verdict(PPY_LAUNCHER_PATTERN, True, "the runtime's own launcher, by path")
    if "/" in program:
        return Verdict(suggestion, False, "a program named by path is never learned")
    if program in NEVER:
        return Verdict(suggestion, False, f"{program} is never learned")
    kind = SAFE_FAMILY.get(program)
    if kind is None:
        return Verdict(suggestion, False, f"{program} is not in the safe family")
    args = words[1:]
    paths = [a for a in args if not a.startswith("-")]
    if kind == "write":
        if not worktree:
            return Verdict(suggestion, False, "no worktree to hold the write to")
        strict = program == "rm"
        if not paths or not all(_inside(p, worktree, strict=strict) for p in paths):
            return Verdict(suggestion, False, f"{program} reaches outside the worktree")
    elif program == "find":
        if _FIND_ACTIONS & set(args):
            return Verdict(suggestion, False, "find with an action that runs or deletes")
    elif program == "sed":
        in_place = any(a.startswith("-i") or a.startswith("--in-place") for a in args)
        if in_place and (not worktree or not all(_inside(p, worktree) for p in paths[1:])):
            return Verdict(suggestion, False, "sed -i edits a file outside the worktree")
    elif program == "awk":
        if any(">" in a or "|" in a or "system" in a for a in args):
            return Verdict(suggestion, False, "awk that writes or runs a command")
    return Verdict(suggestion, True, f"{program} is in the safe family ({kind})")


def learn(
    denials: list[dict],
    *,
    task_id: int | None,
    run_id: int | None,
    worktree: str | None,
) -> list[dict]:
    """Record each denial, then let the runtime apply what it may. Never raises."""
    if not denials:
        return []
    from papaya_agent_runtime import config_changes
    from papaya_agent_runtime.state import init_db, store

    try:
        conn = init_db()
        learnable_seen = False
        for denial in denials:
            tool = str(denial.get("tool_name") or denial.get("tool") or "")
            tool_input = denial.get("tool_input") or {}
            command = tool_input.get("command") if isinstance(tool_input, dict) else None
            verdict = classify(tool, command, worktree)
            learnable_seen = learnable_seen or verdict.in_family
            store.append_event(
                conn,
                kind=PERMISSION_DENIED,
                payload={
                    "tool": tool,
                    "command": command,
                    "pattern": verdict.pattern,
                    "in_family": verdict.in_family,
                    "reason": verdict.reason,
                    "worktree": worktree,
                },
                run_id=run_id,
                task_id=task_id,
            )
    except Exception:  # noqa: BLE001 - a worker's turn must end whatever this does
        return []
    if not learnable_seen:
        return []
    return config_changes.apply(context=f"learned from task {task_id}'s denials")


def _denials(in_family: bool) -> list[dict]:
    from papaya_agent_runtime.state import init_db

    rows = (
        init_db()
        .execute(
            "SELECT id, task_id, payload FROM events WHERE kind = ? ORDER BY id",
            (PERMISSION_DENIED,),
        )
        .fetchall()
    )
    seen: dict[str, dict] = {}
    for row in rows:
        payload = json.loads(row["payload"])
        if bool(payload.get("in_family")) != in_family:
            continue
        key = payload.get("pattern") or payload.get("command") or ""
        seen.setdefault(
            key,
            {"event_id": int(row["id"]), "task_id": row["task_id"], **payload},
        )
    return list(seen.values())


def learnable() -> list[dict]:
    """Safe-family denials, one per pattern (the first one seen is the evidence)."""
    try:
        return [d for d in _denials(True) if d.get("pattern")]
    except Exception:  # noqa: BLE001 - an unreadable state db is reported elsewhere
        return []


def refused() -> list[dict]:
    """Denials outside the family, one per pattern."""
    try:
        return _denials(False)
    except Exception:  # noqa: BLE001
        return []


__all__ = [
    "NEVER",
    "PERMISSION_DENIED",
    "SAFE_FAMILY",
    "Verdict",
    "classify",
    "learn",
    "learnable",
    "refused",
]
