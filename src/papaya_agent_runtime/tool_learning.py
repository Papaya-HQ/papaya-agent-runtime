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

**A denial has one of three kinds**, and only one of them is about the profile. The
self-report's first night opened "denied `Bash(cd:*)`" for `cd <worktree> && grep`,
which the profile allows and the command rules refuse for its shape, and "denied
`Bash(docker:*)`" for a worker querying a container it was told was not its own:

- ``command_shape``: operators, a pipe, redirection, substitution, an inline
  environment assignment. The worker broke the command rules. After two on one worker
  it is steered once with the rules themselves; three workers in one repository in a
  day is a `prompt-clarity` deficiency about the rules text.
- ``policy_refusal``: a program the NEVER list or the environment block keeps from
  workers. Counted, never reported; the worker is steered once with the rule.
- ``profile_gap``: a plain command the profile did not let through. Learned when it
  is in the safe family; a gap learning cannot close is a `worker-denial` deficiency.

The harness reports one denial on two paths (a live `permission_denied` stream line
and the turn's `result`), and a worker often retries a refused line as it was, so a
denial is recorded once per tool call id and once per exact command a minute.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from papaya_agent_runtime.config import PPY_LAUNCHER_PATTERN

log = logging.getLogger("papaya_agent_runtime.tool_learning")

#: A worker's refused tool call.
PERMISSION_DENIED = "permission_denied"
#: The runtime steered a worker about its denials (payload ``kind``, ``program``).
DENIAL_STEER = "denial_steer"

#: The command broke the command rules: one plain command per call.
COMMAND_SHAPE = "command_shape"
#: The program is one workers are never given.
POLICY_REFUSAL = "policy_refusal"
#: A plain command the profile did not allow.
PROFILE_GAP = "profile_gap"
#: A command the worker is allowed to run, refused for WHERE it pointed: outside the
#: session's own worktree. Nothing about the profile would change that.
OUTSIDE_WORKTREE = "outside_worktree"
#: The harness allowed the call and the TARGET REPOSITORY's own tool hook refused it.
#: Nothing about the profile, the shape or the policy would change that, and the only
#: thing that makes the command run is whatever the hook wants (issues #83, #116).
HOOK_REFUSAL = "hook_refusal"
KINDS = (COMMAND_SHAPE, POLICY_REFUSAL, PROFILE_GAP, OUTSIDE_WORKTREE, HOOK_REFUSAL)

#: Command-shape denials on one worker before it is steered with the rules.
SHAPE_STEER_AFTER = 2
#: Two records of one exact command on one task this close together are one denial.
DEDUPE_SECONDS = 60.0

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
    "ps",
    "rg",
    "shasum",
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
    "nvm",
    "pnpm",
    "pytest",
    "python",
    "python3",
    "ruff",
    "uv",
    "xcodebuild",
    "xcodegen",
    # The Xcode toolchain's front door: simctl for simulator captures, swift, the SDK paths.
    "xcrun",
)
#: The browser a worker checks its own UI in: screenshots, console, the page it serves.
BROWSER = ("chrome-devtools-axi",)
#: File verbs, learned only from a call whose every path is inside the worktree.
WORKTREE_WRITES = ("cp", "mkdir", "mv", "rm", "tee", "touch")
#: Read-only unless told otherwise; the checks in :func:`classify` refuse the rest.
CONDITIONAL = ("awk", "find", "sed")

#: The safe family: program -> kind.
SAFE_FAMILY: dict[str, str] = {
    **dict.fromkeys(READ_ONLY, "read"),
    **dict.fromkeys(TOOLCHAINS, "run"),
    **dict.fromkeys(BROWSER, "run"),
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
        # The provisioned wrapper of `gh`: the forge is the runtime's, never a worker's.
        "gh-axi",
        # Stopping processes is the supervisor's; a worker's own run stops on its own.
        "kill",
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

#: Programs the environment block keeps for the runtime: a task's database stack is
#: named there, and the shared containers are not a worker's to start, stop or query.
ENVIRONMENT_FORBIDS = frozenset({"docker", "docker-compose", "podman"})
#: A denial of one of these is the worker breaking a rule, not a gap in its profile.
POLICY = NEVER | ENVIRONMENT_FORBIDS

_FIND_ACTIONS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fls"})
_OPERATORS = set(";&|<>`\n")


@dataclass(frozen=True)
class Verdict:
    """What a denied tool call means for the profile."""

    #: The pattern that would have allowed it (learned when ``in_family``).
    pattern: str
    in_family: bool
    reason: str
    #: One of :data:`KINDS`.
    kind: str = PROFILE_GAP
    #: The program the command runs, when it could be read.
    program: str = ""
    #: For :data:`HOOK_REFUSAL`: the repository's hook script, when it could be read.
    hook: str = ""
    #: For :data:`HOOK_REFUSAL`: the settings file registering that hook.
    hook_settings: str = ""
    #: The diagnosis rests on the absence of the harness's own refusal line rather
    #: than on a hook the runtime could actually read. Said in the recorded reason
    #: and in the issue, because it is the one part that is deduced, not observed.
    inferred: bool = False


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


def _reaches_out(paths: list[str], worktree: str) -> bool:
    """Every path this read names sits outside the worktree.

    All of them, not any: a command reading one file in the worktree and one
    outside it was still refused for the shape of the profile, and a mixed case is
    not clear enough evidence to stop learning from.
    """
    real = [p for p in paths if not p.startswith("-")]
    return bool(real) and not any(_inside(p, worktree) for p in real)


def _suggest(program: str) -> str:
    return f"Bash({program}:*)"


_SHAPE_REASON = (
    "a compound, piped or redirected command is refused by the command rules; "
    "split it into one command per call"
)


def _program_of(command: str) -> str:
    try:
        words = shlex.split(command)
    except ValueError:
        return ""
    return words[0] if words else ""


def _hook_verdict(tool: str, command: str, worktree: str | None) -> Verdict:
    """The target repository's own hook refused a call the harness allowed.

    The profile, the shape, the policy and the location are all beside the point:
    the call got past every one of them. What the hook wants is the only thing that
    would let the command run, and the hook's own words are in the evidence.
    """
    from papaya_agent_runtime.providers.claude import registered_hooks

    program = _program_of(command)
    hooks = registered_hooks(worktree, tool)
    hook = hooks[0] if hooks else {}
    script = str(hook.get("script") or hook.get("command") or "")
    settings = str(hook.get("settings") or "")
    if script:
        reason = (
            f"the repository's own `{script}` hook, registered as a {tool} `PreToolUse` "
            f"hook in `{settings}`, refused it; the harness allowed it. Nothing about the "
            "worker's tool profile would change this"
        )
    else:
        reason = (
            f"the harness never refused it — no `permission_denied` line names this call — "
            f"so something the repository runs between permission and execution did. This "
            f"repository registers no readable {tool} `PreToolUse` hook, so the diagnosis is "
            "inferred from the absence of the harness's own refusal"
        )
    return Verdict(
        _suggest(program) if program else "",
        False,
        reason,
        HOOK_REFUSAL,
        program,
        hook=script,
        hook_settings=settings,
        inferred=not script,
    )


def classify(
    tool: str,
    command: str | None,
    worktree: str | None,
    *,
    refusal: dict | None = None,
) -> Verdict:
    """Which kind of denial this is, and which pattern would have allowed it.

    ``refusal`` is the provider's evidence about WHO refused the call
    (`providers.base.REFUSAL_FIELDS`). It is judged first and settles the question
    outright: a call the harness never refused got past the profile, the shape rules
    and the policy, so only the repository's own hook is left. Without it — an older
    record, a provider that reports no such thing — the command alone is judged,
    exactly as before.

    Then the shape is judged before the program: `cd x && grep y` names `cd`, which
    the profile has, and was refused for the `&&`.
    """
    if tool != "Bash":
        return Verdict(tool, False, f"{tool} is not a shell command; only Bash is learned")
    command = (command or "").strip()
    if refusal is not None and not refusal.get("harness_line", True):
        return _hook_verdict(tool, command, worktree)
    bare = _unquoted(command)
    if not command or bare is None:
        return Verdict("", False, "the command could not be read", COMMAND_SHAPE)
    try:
        words = shlex.split(command)
    except ValueError:
        return Verdict("", False, "the command could not be read", COMMAND_SHAPE)
    program = words[0] if words else ""
    suggestion = _suggest(program)
    if _OPERATORS & set(bare) or "$(" in bare:
        return Verdict(suggestion, False, _SHAPE_REASON, COMMAND_SHAPE, program)
    if "=" in program and not program.startswith(("/", ".")):
        return Verdict(
            "", False, "an inline environment assignment is never learned", COMMAND_SHAPE
        )
    if program == "ppy" or program == "./bin/ppy":
        return Verdict(suggestion, True, "the runtime's own launcher", program=program)
    if program.endswith("/bin/ppy"):
        return Verdict(
            PPY_LAUNCHER_PATTERN, True, "the runtime's own launcher, by path", program=program
        )
    name = os.path.basename(program)
    if name in NEVER:
        return Verdict(suggestion, False, f"{name} is never learned", POLICY_REFUSAL, name)
    if name in ENVIRONMENT_FORBIDS:
        return Verdict(
            suggestion,
            False,
            f"{name} is kept from workers by the environment block",
            POLICY_REFUSAL,
            name,
        )
    if "/" in program:
        return Verdict(suggestion, False, "a program named by path is never learned", program=name)
    family = SAFE_FAMILY.get(program)
    if family is None:
        return Verdict(suggestion, False, f"{program} is not in the safe family", program=program)
    args = words[1:]
    paths = [a for a in args if not a.startswith("-")]
    if family == "read" and worktree and paths and _reaches_out(paths, worktree):
        # `ls ../other-repo` is not a missing `ls`: the harness confines a session to
        # its working directory, and adding the program to the profile would change
        # nothing. Learning it as a gap taught the wrong lesson twice (2026-09-17,
        # task 30). The manager grants the directory instead, with
        # `ppy reference grant` for a registered repository.
        return Verdict(
            "",
            False,
            f"{program} pointed outside the worktree, which no tool pattern allows; a "
            "registered repository is granted with `ppy reference grant`",
            OUTSIDE_WORKTREE,
            program,
        )
    refusal = ""
    if family == "write":
        if not worktree:
            refusal = "no worktree to hold the write to"
        elif not paths or not all(_inside(p, worktree, strict=program == "rm") for p in paths):
            refusal = f"{program} reaches outside the worktree"
    elif program == "find":
        if _FIND_ACTIONS & set(args):
            refusal = "find with an action that runs or deletes"
    elif program == "sed":
        in_place = any(a.startswith("-i") or a.startswith("--in-place") for a in args)
        if in_place and (not worktree or not all(_inside(p, worktree) for p in paths[1:])):
            refusal = "sed -i edits a file outside the worktree"
    elif program == "awk" and any(">" in a or "|" in a or "system" in a for a in args):
        refusal = "awk that writes or runs a command"
    if refusal:
        return Verdict(suggestion, False, refusal, program=program)
    return Verdict(suggestion, True, f"{program} is in the safe family ({family})", program=program)


#: How much of a hook's own output is kept as evidence and shown to a person.
HOOK_SAID_LINES = 20


def _hook_said(refusal: dict | None) -> str:
    """The hook's own last lines, which are what a person needs to act on."""
    text = str((refusal or {}).get("tool_result") or "").strip()
    if not text:
        return ""
    # Claude Code prefixes the hook's stderr on newer releases and not on older
    # ones; strip it when it is there so the evidence reads the same either way.
    for prefix in ("Error: PreToolUse:", "PreToolUse:", "Error: "):
        if text.startswith(prefix):
            text = text[len(prefix) :].lstrip()
            if text.lower().startswith(("bash hook error:", "hook error:")):
                text = text.split(":", 1)[1].lstrip()
            break
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(lines[:HOOK_SAID_LINES])


def _command(denial: dict) -> str | None:
    tool_input = denial.get("tool_input") or {}
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    return str(command) if command is not None else None


def _tool(denial: dict) -> str:
    return str(denial.get("tool_name") or denial.get("tool") or "")


def _is_duplicate(conn, task_id: int | None, tool_use_id: str, command: str | None) -> bool:
    """Already recorded: the same tool call, or the same command on the task a moment ago."""
    now = datetime.now(UTC)
    rows = conn.execute(
        "SELECT payload, created_at FROM events WHERE kind = ? AND task_id IS ? ORDER BY id DESC",
        (PERMISSION_DENIED, task_id),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if tool_use_id and payload.get("tool_use_id") == tool_use_id:
            return True
        if command is None or payload.get("command") != command:
            continue
        # A retry of the same line is a new tool call id and the same refusal.
        try:
            at = datetime.fromisoformat(str(row["created_at"]))
        except ValueError:
            continue
        if abs((now - at).total_seconds()) <= DEDUPE_SECONDS:
            return True
    return False


def _full_suite_verdict(
    conn, task_id: int | None, tool: str, command: str | None
) -> Verdict | None:
    """A refusal of the task's repository's full suite: the policy, not a profile gap."""
    from papaya_agent_runtime.providers.command_rules import FULL_SUITE_PROGRAM

    if tool != "Bash" or task_id is None or not command:
        return None
    row = conn.execute(
        "SELECT r.full_suite_command FROM tasks t JOIN repos r ON r.id = t.repo_id WHERE t.id = ?",
        (task_id,),
    ).fetchone()
    full = " ".join(str((row["full_suite_command"] if row else "") or "").split())
    if not full or " ".join(command.split()) != full:
        return None
    return Verdict(
        "",
        False,
        "the repository's full suite runs once at the delivered head, not as a tool call",
        POLICY_REFUSAL,
        FULL_SUITE_PROGRAM,
    )


def record(
    denials: Iterable[dict],
    *,
    task_id: int | None,
    run_id: int | None,
    worktree: str | None,
) -> list[dict]:
    """Record each denial not already on the task; returns the payloads written. May raise."""
    from papaya_agent_runtime.state import init_db, store

    written: list[dict] = []
    conn = init_db()
    try:
        for denial in denials:
            if not isinstance(denial, dict):
                continue
            tool, command = _tool(denial), _command(denial)
            tool_use_id = str(denial.get("tool_use_id") or "")
            if _is_duplicate(conn, task_id, tool_use_id, command):
                continue
            refusal = denial.get("refusal") if isinstance(denial.get("refusal"), dict) else None
            # A hook block is judged before the full-suite rule: the repository
            # refused this call, whatever the command happened to be.
            verdict = classify(tool, command, worktree, refusal=refusal)
            if verdict.kind != HOOK_REFUSAL:
                verdict = _full_suite_verdict(conn, task_id, tool, command) or verdict
            payload = {
                "tool": tool,
                "tool_use_id": tool_use_id or None,
                "command": command,
                "pattern": verdict.pattern,
                "in_family": verdict.in_family,
                "kind": verdict.kind,
                "program": verdict.program,
                "reason": verdict.reason,
                "worktree": worktree,
            }
            if verdict.kind == HOOK_REFUSAL:
                payload["hook"] = verdict.hook
                payload["hook_settings"] = verdict.hook_settings
                payload["inferred"] = verdict.inferred
                payload["hook_said"] = _hook_said(refusal)
            store.append_event(
                conn, kind=PERMISSION_DENIED, payload=payload, run_id=run_id, task_id=task_id
            )
            written.append(payload)
    finally:
        conn.close()
    return written


def learn(
    denials: list[dict],
    *,
    task_id: int | None,
    run_id: int | None,
    worktree: str | None,
    branch: str | None = None,
) -> list[dict]:
    """Record each new denial, steer and report what it means, then learn. Never raises.

    Called for each live denial while the worker runs and again with the turn's
    list when it ends; a denial seen on both paths is recorded, steered and reported
    once.
    """
    if not denials:
        return []
    from papaya_agent_runtime import config_changes, deficiencies

    try:
        profile = _profile()
        new = record(denials, task_id=task_id, run_id=run_id, worktree=worktree)
        if not new:
            return []
        if task_id is not None:
            _steer_about(task_id, run_id, new, branch)
            deficiencies.record_denials(
                [_as_denial(p) for p in new],
                task_id=task_id,
                run_id=run_id,
                worktree=worktree,
                profile=profile,
            )
    except Exception as exc:  # noqa: BLE001 - a worker's turn must end whatever this does
        log.warning("[tool_learning] Could not record task %s's denials: %s", task_id, exc)
        return []
    if task_id is not None:
        _request_capabilities(task_id, new)
    if not any(p["in_family"] for p in new):
        return []
    return config_changes.apply(context=f"learned from task {task_id}'s denials")


def _request_capabilities(task_id: int, new: list[dict]) -> None:
    """A plain command the profile refused, outside the safe family, is a request.

    The worker does not have to ask twice: the refusal itself is the need, decided by
    this install's policy and, when policy cannot, put in front of a person
    (`capability_requests`). Never raises.
    """
    from papaya_agent_runtime import capability_requests

    for payload in new:
        program = str(payload.get("program") or "")
        if payload.get("kind") != PROFILE_GAP or payload.get("in_family") or not program:
            continue
        try:
            capability_requests.request(
                task_id,
                program,
                why="",
                source=capability_requests.DENIAL,
                command=payload.get("command"),
            )
        except Exception as exc:  # noqa: BLE001 - a request that cannot be made is logged
            log.warning(
                "[tool_learning] Could not request %s for task %s: %s", program, task_id, exc
            )


def _as_denial(payload: dict) -> dict:
    """The recorded denial, shaped back for the ledger, carrying the decided kind.

    The ledger used to re-run `classify` on the command alone, which is now short of
    what decided the kind: the refusal evidence is gone by then. Handing the verdict
    over keeps the two from ever disagreeing about which denial is which.
    """
    return {
        "tool_name": payload.get("tool"),
        "tool_use_id": payload.get("tool_use_id"),
        "tool_input": {"command": payload.get("command")},
        "verdict": {
            "kind": payload.get("kind"),
            "pattern": payload.get("pattern"),
            "in_family": payload.get("in_family"),
            "program": payload.get("program"),
            "reason": payload.get("reason"),
            "hook": payload.get("hook", ""),
            "hook_settings": payload.get("hook_settings", ""),
            "hook_said": payload.get("hook_said", ""),
            "inferred": payload.get("inferred", False),
        },
    }


def _profile() -> set[str] | None:
    """The worker tools a dispatch gets today, or ``None`` when there is no config to read."""
    from papaya_agent_runtime.config import effective_claude_tools, load_config
    from papaya_agent_runtime.paths import config_path

    try:
        if not config_path().exists():
            return None
        return set(effective_claude_tools(load_config()))
    except Exception:  # noqa: BLE001 - an unreadable config is reported by readiness
        return None


# ── steering the worker ─────────────────────────────────────────────────────


def steer_worker(task_id: int, message: str) -> None:
    """Deliver ``message`` through the supervisor, off the caller's thread. Never raises.

    The runner calls this from the loop reading the worker's output, which must not
    wait on a socket. Tests replace it.
    """

    def deliver() -> None:
        from papaya_agent_runtime.state import store
        from papaya_agent_runtime.supervisor.client import SupervisorClient

        try:
            SupervisorClient().steer_task(task_id, message, by=store.BY_MANAGER)
        except Exception as exc:  # noqa: BLE001 - a steer that cannot land is logged
            log.warning("[tool_learning] Could not steer task %s: %s", task_id, exc)

    threading.Thread(target=deliver, name=f"ppy-denial-steer-{task_id}", daemon=True).start()


def shape_steer_message(commands: list[str], branch: str | None) -> str:
    """The rewrite for what was actually refused, then the rules it broke.

    The rules alone were what this steer used to say, and workers kept writing the
    same shapes back (issue #121: 30 occurrences, 16 of them `cd <worktree> && …`).
    A worker that has just been refused needs the command to run instead, so the
    replacement for each shape it used comes first and the rules come after it.
    """
    from papaya_agent_runtime.providers.command_rules import (
        command_rules,
        rewrite_table,
        rewrites_for,
    )

    shown = "\n".join(f"- `{c}`" for c in commands)
    return (
        "These shell commands were refused for their shape, not for the program they ran, "
        "and the runtime will not add a tool for them:\n\n"
        f"{shown}\n\n"
        "Run these instead — your shell already starts in your worktree, so there is "
        "nothing to `cd` into to reach your own files:\n\n"
        f"{rewrite_table(rewrites_for(commands))}\n\n"
        "Every command you run is held to these rules:\n\n" + command_rules("claude", branch)
    )


def policy_rule(program: str) -> str:
    """The rule a refused ``program`` broke, in one sentence."""
    from papaya_agent_runtime.providers.command_rules import (
        FULL_SUITE_PROGRAM,
        FULL_SUITE_REFUSAL,
    )

    if program == FULL_SUITE_PROGRAM:
        return FULL_SUITE_REFUSAL
    if program in ("gh", "gh-axi"):
        return (
            f"A worker never runs `{program}`: pushing, opening, reading and merging pull "
            "requests is the runtime's. If you need a pull request's state, say what in your "
            "progress report and it is supplied."
        )
    if program in ENVIRONMENT_FORBIDS:
        return (
            f"A worker does not run `{program}` here: when this repository has a database "
            "stack, the environment block of your brief names the only commands that touch "
            "it, and the shared containers are not yours to start, stop or query."
        )
    return f"`{program}` is never available to a worker, and the runtime will not add it."


def hook_steer_message(payload: dict) -> str:
    """What the repository's hook refused, in its own words, and what to do now.

    A push is the case that matters: the worker finished, ran exactly the command
    its rules prescribe, and the repository stopped it. Retrying is pointless and
    weakening the hook is forbidden, so the worker is told the one true thing —
    commit, report, and let the runtime push after its own gate.
    """
    from papaya_agent_runtime.providers.command_rules import (
        FLAGGED_RULE,
        RUNTIME_PUSHES_RULE,
    )

    command = str(payload.get("command") or "")
    hook = str(payload.get("hook") or "")
    said = str(payload.get("hook_said") or "")
    named = f"the repository's own `{hook}` hook" if hook else "a hook this repository runs"
    lines = [
        f"`{command}` was refused by {named}, not by your tool profile and not for "
        "its shape. The harness allowed the call; the repository stopped it.",
    ]
    if said:
        lines.append(f"What the hook said:\n\n```\n{said}\n```")
    if _is_push(command):
        lines.append(RUNTIME_PUSHES_RULE)
    else:
        lines.append(
            "Do not retry it and do not work around the hook — never with `--no-verify`, "
            "never by editing or disabling it. Either do what the hook asks, or record it "
            "below and carry on."
        )
    return "\n\n".join([*lines, FLAGGED_RULE])


def _is_push(command: str) -> bool:
    """A `git push`, however it was written. Matches the hook's own whole-word test."""
    return bool(re.search(r"(?:^|[^A-Za-z0-9_-])git\s+push(?:[^A-Za-z0-9_-]|$)", command))


def policy_steer_message(program: str, command: str) -> str:
    """The rule it broke, then what to do with the refused command."""
    from papaya_agent_runtime.providers.command_rules import FLAGGED_RULE

    return f"`{command}` was refused. {policy_rule(program)}\n\n{FLAGGED_RULE}"


def _steer_about(task_id: int, run_id: int | None, new: list[dict], branch: str | None) -> None:
    """At most one rules steer per worker, and one per refused program."""
    from papaya_agent_runtime.state import init_db, store

    steers: list[str] = []
    conn = init_db()
    try:
        rows = conn.execute(
            "SELECT kind, payload FROM events WHERE task_id = ? AND kind IN (?, ?) ORDER BY id",
            (task_id, PERMISSION_DENIED, DENIAL_STEER),
        ).fetchall()
        payloads = [(row["kind"], json.loads(row["payload"])) for row in rows]
        steered = {
            (p.get("kind"), p.get("program") or "") for kind, p in payloads if kind == DENIAL_STEER
        }
        shapes = [
            str(p.get("command") or "")
            for kind, p in payloads
            if kind == PERMISSION_DENIED and p.get("kind") == COMMAND_SHAPE
        ]
        if (
            any(p["kind"] == COMMAND_SHAPE for p in new)
            and len(shapes) >= SHAPE_STEER_AFTER
            and (COMMAND_SHAPE, "") not in steered
        ):
            steered.add((COMMAND_SHAPE, ""))
            store.append_event(
                conn,
                kind=DENIAL_STEER,
                payload={"kind": COMMAND_SHAPE, "commands": shapes},
                run_id=run_id,
                task_id=task_id,
            )
            steers.append(shape_steer_message(shapes, branch))
        for payload in new:
            # One steer per hook, not per refused command: a worker that keeps
            # meeting the same hook has already been told the one thing to do.
            hook = str(payload.get("hook") or "")
            if payload["kind"] == HOOK_REFUSAL and (HOOK_REFUSAL, hook) not in steered:
                steered.add((HOOK_REFUSAL, hook))
                store.append_event(
                    conn,
                    kind=DENIAL_STEER,
                    payload={
                        "kind": HOOK_REFUSAL,
                        "program": hook,
                        "commands": [payload.get("command")],
                    },
                    run_id=run_id,
                    task_id=task_id,
                )
                steers.append(hook_steer_message(payload))
                continue
            program = str(payload.get("program") or "")
            if payload["kind"] != POLICY_REFUSAL or (POLICY_REFUSAL, program) in steered:
                continue
            steered.add((POLICY_REFUSAL, program))
            store.append_event(
                conn,
                kind=DENIAL_STEER,
                payload={
                    "kind": POLICY_REFUSAL,
                    "program": program,
                    "commands": [payload.get("command")],
                },
                run_id=run_id,
                task_id=task_id,
            )
            steers.append(policy_steer_message(program, str(payload.get("command") or "")))
    finally:
        conn.close()
    for message in steers:
        steer_worker(task_id, message)


# ── reading the record ──────────────────────────────────────────────────────


def kind_of(payload: dict) -> str:
    """A recorded denial's kind; a row written before kinds existed is classified now."""
    kind = payload.get("kind")
    if kind in KINDS:
        return str(kind)
    return classify(
        str(payload.get("tool") or ""), payload.get("command"), payload.get("worktree")
    ).kind


def counts(days: int | None = None) -> dict[str, dict[str, int]]:
    """Recorded denials by repository and kind: ``{repo: {kind: n}}``. Never raises.

    ``days`` counts only the last N days, which is what makes a change checkable:
    "did the rewrite table cut `command_shape`?" is a question about this week, not
    about every denial the ledger has ever held.
    """
    return _tally(days, lambda payload: kind_of(payload))


def shape_counts(days: int | None = None) -> dict[str, dict[str, int]]:
    """`command_shape` denials by repository and SHAPE: ``{repo: {shape: n}}``.

    The shape is the rewrite row the command matches, so the tally lines up one for
    one with what the steer tells a worker to run instead — a shape that keeps its
    count after the rewrite text landed is a rewrite that is not landing.
    """

    def shape(payload: dict) -> str | None:
        if kind_of(payload) != COMMAND_SHAPE:
            return None
        from papaya_agent_runtime.providers.command_rules import REWRITES, rewrites_for

        command = str(payload.get("command") or "")
        matched = rewrites_for([command])
        return matched[0][0] if matched is not REWRITES else "other"

    return _tally(days, shape)


def _tally(days: int | None, key: Callable[[dict], str | None]) -> dict[str, dict[str, int]]:
    from papaya_agent_runtime.paths import db_path
    from papaya_agent_runtime.state import init_db

    found: dict[str, dict[str, int]] = {}
    try:
        if not db_path().exists():
            return found
        sql = (
            "SELECT e.payload, r.name FROM events e LEFT JOIN tasks t ON t.id = e.task_id "
            "LEFT JOIN repos r ON r.id = t.repo_id WHERE e.kind = ?"
        )
        args: list[object] = [PERMISSION_DENIED]
        if days is not None:
            since = datetime.now(UTC) - timedelta(days=days)
            sql += " AND e.created_at >= ?"
            args.append(since.isoformat())
        conn = init_db()
        try:
            rows = conn.execute(sql, args).fetchall()
        finally:
            conn.close()
        for row in rows:
            bucket = key(json.loads(row["payload"]))
            if bucket is None:
                continue
            per_repo = found.setdefault(str(row["name"] or "?"), {})
            per_repo[bucket] = per_repo.get(bucket, 0) + 1
    except Exception:  # noqa: BLE001 - diagnostics must not crash
        return {}
    return found


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
        if not in_family and kind_of(payload) != PROFILE_GAP:
            continue  # the rules or the policy refused it; no pattern would help
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
    """Profile gaps outside the family, one per pattern."""
    try:
        return _denials(False)
    except Exception:  # noqa: BLE001
        return []


__all__ = [
    "COMMAND_SHAPE",
    "DENIAL_STEER",
    "ENVIRONMENT_FORBIDS",
    "HOOK_REFUSAL",
    "KINDS",
    "NEVER",
    "PERMISSION_DENIED",
    "POLICY",
    "OUTSIDE_WORKTREE",
    "POLICY_REFUSAL",
    "PROFILE_GAP",
    "SAFE_FAMILY",
    "Verdict",
    "classify",
    "counts",
    "hook_steer_message",
    "kind_of",
    "learn",
    "learnable",
    "record",
    "refused",
    "steer_worker",
]
