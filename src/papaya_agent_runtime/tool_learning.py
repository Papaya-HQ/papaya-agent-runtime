"""Learn the Claude worker's tools from what workers are actually denied.

PAP-219's worker was refused `python3 -c`, a `cp`, and every compound command, and the
runtime watched it happen: the denials were in the worker's own result event and
nothing read them. Now every denial is a `permission_denied` event, and a denied
command in the **safe family** below adds its pattern to `claude.extra_tools` (or
restores it from `claude.dropped_tools`), recorded as a `config_change` with the
denial as evidence, so the next dispatch has it.

The safe family is a closed allowlist, and the only thing ever added is
`Bash(<program>:*)` for a program on it. The code holds the default list and the
install tunes it in its `capabilities` config (`safe_family` adds, `drop_family`
removes, and a person's approval adds the approved program), but the list stays closed:
a program on :data:`POLICY` is never in it, whatever the config says, and a versioned
spelling of a family program (`python3.12`) is the same program. Nothing else is ever
learned: no `Bash(*)`,
no `sudo`, no network tool, no command named by an arbitrary path, no compound or
redirected command, and no file verb whose target is outside the worker's worktree.
A gap outside the family is a capability request (`capability_requests`): a program,
a program named by path (decided by where it resolves, granted as that path), or a
tool that is not the shell (granted by its name).

A learned pattern is a prefix, so once `Bash(cp:*)` is learned from a `cp` inside the
worktree it matches any `cp`. That is the same trade the documented profile already
makes: containment is the worker's write boundary, not this verb list.

**A denial has one of three kinds**, and only one of them is about the profile. The
self-report's first night opened "denied `Bash(cd:*)`" for `cd <worktree> && grep`,
which the profile allows and the command rules refuse for its shape, and "denied
`Bash(docker:*)`" for a worker querying a container it was told was not its own:

- ``command_shape``: operators, a pipe, redirection, substitution, an inline
  environment assignment, a shell builtin that changes the shell, a quoted argument
  the harness will not analyse. The worker broke the command rules. After two on one worker
  it is steered once with the rules themselves; three workers in one repository in a
  day is a `prompt-clarity` deficiency about the rules text.
- ``policy_refusal``: a program the NEVER list or the environment block keeps from
  workers. Counted, never reported; the worker is steered once with the rule.
- ``profile_gap``: a plain command the profile did not let through. Learned when it
  is in the safe family; a gap learning cannot close is a capability request, and
  only one no request carries is a `worker-denial` deficiency.

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
from papaya_agent_runtime.providers import command_rules

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

#: The code's safe family: program -> kind. It is the start, not the last word: an
#: install tunes it in `capabilities.safe_family` / `capabilities.drop_family` (see
#: :func:`safe_family`), and what classification reads is that effective family.
DEFAULT_SAFE_FAMILY: dict[str, str] = {
    **dict.fromkeys(READ_ONLY, "read"),
    **dict.fromkeys(TOOLCHAINS, "run"),
    **dict.fromkeys(BROWSER, "run"),
    **dict.fromkeys(WORKTREE_WRITES, "write"),
    **dict.fromkeys(CONDITIONAL, "conditional"),
}
SAFE_FAMILY = DEFAULT_SAFE_FAMILY
#: The kinds an install may give a program it adds: how it is checked. `conditional`
#: is the code's own (find, sed, awk have per-program argument checks).
FAMILY_KINDS = ("read", "run", "write")

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

#: Shell builtins that change the worker's own shell. None is a program a grant could
#: add: each call runs in a fresh shell, so the change would not outlive the call, and
#: PATH and the rest of the environment come from the runtime's environment block. A
#: denial of one is a SHAPE, answered with what to do instead (issue #140: three
#: `export PATH=…` denials became requests for a program called `export`). `eval`,
#: `exec` and `env` are not here: they run other commands and stay on :data:`NEVER`.
SHELL_BUILTINS = frozenset(
    {
        ".",
        "alias",
        "declare",
        "export",
        "readonly",
        "set",
        "source",
        "typeset",
        "ulimit",
        "umask",
        "unalias",
        "unset",
    }
)

#: The harness will not analyse a quoted argument holding one of these, whatever the
#: program (its own reasons: "Contains simple_expansion", "Contains brace with quote
#: character", "Newline followed by # inside a quoted argument"). Issue #130: seven
#: `ppy progress --note "…"` refusals, every one of them a note with a backtick, a
#: `$(`, a brace or a second line in it.
QUOTED_HAZARDS = ("\n", "`", "$", "{")

_FIND_ACTIONS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fls"})
_OPERATORS = set(";&|<>`\n")
_VERSION_SUFFIX = re.compile(r"[-.]?\d+(?:\.\d+)*$")


def capability_policy():
    """This install's `capabilities` block; the code's defaults when there is no readable config."""
    from papaya_agent_runtime.config import CapabilityPolicy, load_config
    from papaya_agent_runtime.paths import config_path

    try:
        if config_path().exists():
            return load_config().capabilities
    except Exception:  # noqa: BLE001 - an unreadable config falls back to the code's policy
        pass
    return CapabilityPolicy()


def safe_family(policy=None) -> dict[str, str]:
    """The family this install learns from: the code's, plus and minus its config.

    Closed whatever the config says: a program on :data:`POLICY` is never in it, so a
    hand-edited file cannot make `sudo` or `curl` learnable.
    """
    policy = policy or capability_policy()
    family = dict(DEFAULT_SAFE_FAMILY)
    family.update(
        {p: k for p, k in policy.safe_family.items() if k in FAMILY_KINDS and p not in POLICY}
    )
    for program in policy.drop_family:
        family.pop(program, None)
    return family


def _stem(program: str) -> str:
    return _VERSION_SUFFIX.sub("", program)


def variant_of(program: str, known: Iterable[str], never: Iterable[str] = ()) -> str | None:
    """The known program ``program`` is a versioned spelling of (`python3.12` of `python3`).

    Same tool, another version: `xcodebuild`'s and `python`'s numbered siblings do not
    change what a person granted. A program whose stem is on :data:`POLICY` (or
    ``never``) is never a variant of anything.
    """
    stem = _stem(program)
    if not stem or stem == program or stem in POLICY or stem in set(never):
        return None
    return next((k for k in sorted(known) if k != program and _stem(k) == stem), None)


def family_kind(
    program: str, family: dict[str, str], *, variants: bool = True
) -> tuple[str, str] | None:
    """``(kind, base)`` for a program in ``family``; ``base`` is the program it is a variant of."""
    if program in family:
        return family[program], ""
    # `conditional` programs are judged by name in `classify`, so a spelling of one has
    # no checks to inherit and is not a variant.
    base = variant_of(program, [p for p, k in family.items() if k != "conditional"])
    return (family[base], base) if variants and base else None


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
    #: For a program named by path: the path as it was run, which is the pattern's prefix.
    path: str = ""
    #: Where that path resolved, or ``arguments`` for a safe program refused for what it
    #: was asked to do (`capability_requests.IN_WORKTREE` and its siblings).
    reach: str = ""
    #: The path fully resolved.
    resolved: str = ""


def _quoted_hazard(command: str) -> str:
    """The first character the harness will not analyse inside a quoted argument, or ""."""
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
            elif ch in QUOTED_HAZARDS:
                return ch
            continue
        if ch in "'\"":
            quote = ch
    return ""


def path_reach(program: str, worktree: str | None, roots: Iterable[str] = ()) -> tuple[str, str]:
    """Where a program named by path resolves: ``(reach, resolved path)``.

    ``..`` is normalised first and symlinks are then resolved strictly, so a path that
    leaves the worktree through a link is outside, and one that cannot be resolved at
    all is outside too: nothing is granted on a path nobody could check. ``roots`` are
    the other places a worktree's own tools may live — the repository's base clone,
    which the runtime links a worktree's virtualenv to (`worktree.provision.link_venv`),
    so `.venv/bin/python` resolving there is the worktree's own interpreter.
    """
    from papaya_agent_runtime.capability_requests import ABSOLUTE, IN_WORKTREE, OUTSIDE

    if program.startswith("~") or os.path.isabs(program):
        return ABSOLUTE, os.path.normpath(os.path.expanduser(program))
    if not worktree:
        return OUTSIDE, program
    root = os.path.normpath(worktree)
    joined = os.path.normpath(os.path.join(root, program))
    if not joined.startswith(root + os.sep):
        return OUTSIDE, joined
    try:
        resolved = os.path.realpath(joined, strict=True)
    except OSError:
        return OUTSIDE, joined
    for place in (root, *roots):
        try:
            base = os.path.realpath(place, strict=True)
        except OSError:
            continue
        if resolved.startswith(base + os.sep):
            return IN_WORKTREE, resolved
    return OUTSIDE, resolved


def _profiled(program: str) -> bool:
    """Whether the code's own profile already allows ``program`` by name."""
    from papaya_agent_runtime import config

    return f"Bash({program}:*)" in config.CLAUDE_PROFILE


def _tool_verdict(tool: str) -> Verdict:
    """A tool that is not the shell: a capability by its own name, or a place it pointed."""
    from papaya_agent_runtime import capability_requests, config

    if tool in config.CLAUDE_PROFILE:
        # The worker has the tool; the harness refused where it pointed (a Read or a
        # Glob outside the session's directories). No grant of the tool changes that.
        return Verdict(
            "",
            False,
            f"the worker has {tool}; the harness refused where it pointed, outside the worktree",
            OUTSIDE_WORKTREE,
            tool,
        )
    if not capability_requests.is_tool(tool):
        return Verdict("", False, f"`{tool}` cannot be named as a tool to grant")
    return Verdict(
        tool,
        False,
        f"{tool} is not in the worker's tools; it is asked for as a capability by its name",
        PROFILE_GAP,
        tool,
    )


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
    # A bare number is an option's value (`tail -n 6 <file>`), never a path (#131).
    real = [p for p in paths if not p.startswith("-") and not p.isdigit()]
    return bool(real) and not any(_inside(p, worktree) for p in real)


def _suggest(program: str) -> str:
    return f"Bash({program}:*)"


_SHAPE_REASON = (
    "a compound, piped or redirected command is refused by the command rules; "
    "split it into one command per call"
)

_EVIDENCE_REASON = (
    "copying a session's saved tool output has one sanctioned command, `ppy evidence "
    "add`; `cp` takes any path and stays refused"
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


_BUILTIN_REASON = (
    "{name} changes the worker's own shell, which no tool pattern can give it: each call "
    "runs in a fresh shell, and PATH and the environment come from the runtime's "
    "environment block. Run the worktree's own tools by path or through `uv run`/`npx`/"
    "`pnpm exec`, and ask for a missing program with `ppy need <task id> --capability "
    "<program>`"
)

_HAZARD_REASON = (
    "the harness will not analyse a quoted argument holding a newline, a backtick, `$` "
    "or a brace, however plain the program; write the text to a file and pass the path "
    "(`--note-file`, `--why-file`, `git commit -F`)"
)

_CP_FLAGS_REASON = (
    "the harness asks a person to approve any `cp` with flags, whatever the profile "
    "allows; copy one file per call without flags, or keep a receipt with `ppy "
    "evidence add`"
)


def classify(
    tool: str,
    command: str | None,
    worktree: str | None,
    *,
    refusal: dict | None = None,
    roots: Iterable[str] = (),
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

    A tool that is not the shell is a gap asked for by its own name. A program named
    by path carries its ``reach`` (see :func:`path_reach`; ``roots`` are the other
    places the worktree's own tools may resolve to), and a safe-family program refused
    for its arguments carries ``arguments``: neither is granted by the family alone.
    """
    if tool != "Bash":
        return _tool_verdict(tool)
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
    if command_rules.saved_output_source(command) is not None:
        # Judged before everything, including the operators: a worker copying its own
        # session's saved output has an exact sanctioned replacement (`ppy evidence
        # add`), and neither "split it into two calls" nor "cp reaches outside the
        # worktree" is what it needs to hear. It is emphatically NOT a profile gap:
        # `cp` must stay refused, which is the whole point of issue #127.
        return Verdict(suggestion, False, _EVIDENCE_REASON, COMMAND_SHAPE, program)
    if program in SHELL_BUILTINS:
        # Before the operators: `source .venv/bin/activate && pytest` needs to hear
        # "never activate", not "split it into two calls" and then activate anyway.
        return Verdict("", False, _BUILTIN_REASON.format(name=program), COMMAND_SHAPE, program)
    if _OPERATORS & set(bare) or "$(" in bare:
        return Verdict(suggestion, False, _SHAPE_REASON, COMMAND_SHAPE, program)
    if "=" in program and not program.startswith(("/", ".")):
        return Verdict(
            "", False, "an inline environment assignment is never learned", COMMAND_SHAPE
        )
    launcher = program in ("ppy", "./bin/ppy") or program.endswith("/bin/ppy")
    policy = capability_policy()
    found = family_kind(program, safe_family(policy), variants=policy.intent_grants)
    if (launcher or found or _profiled(program)) and _quoted_hazard(command):
        # The program is allowed or learnable; only the argument can have been refused.
        return Verdict(suggestion, False, _HAZARD_REASON, COMMAND_SHAPE, program)
    if program == "cp" and any(a.startswith("-") for a in words[1:]):
        return Verdict(suggestion, False, _CP_FLAGS_REASON, COMMAND_SHAPE, program)
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
        return _path_verdict(program, name, worktree, roots)
    if found is None:
        return Verdict(suggestion, False, f"{program} is not in the safe family", program=program)
    family, base = found
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
    targets: list[str] = []
    if family == "write":
        if not worktree:
            refusal = "no worktree to hold the write to"
        elif not paths:
            refusal = f"{program} names no path inside the worktree"
        else:
            targets = paths
            if program == "rm" and not all(_inside(p, worktree, strict=True) for p in paths):
                refusal = "rm of the worktree itself"
    elif program == "find":
        if _FIND_ACTIONS & set(args):
            refusal = "find with an action that runs or deletes"
    elif program == "sed":
        in_place = any(a.startswith("-i") or a.startswith("--in-place") for a in args)
        if in_place:
            targets = paths[1:]
            if not worktree:
                refusal = "sed -i with no worktree to hold the edit"
    elif program == "awk" and any(">" in a or "|" in a or "system" in a for a in args):
        refusal = "awk that writes or runs a command"
    if worktree and targets and not all(_inside(p, worktree) for p in targets):
        # A write that leaves the worktree is refused for WHERE it points, like a read
        # that does: learning the verb (or granting it, which is what the request loop
        # did on 2026-09-18, auto-granting `cp`, `mv` and `mkdir` to every worker) would
        # let every later call of it write anywhere.
        return Verdict(
            "",
            False,
            f"{program} writes outside the worktree, which no tool pattern allows; keep "
            "receipts with `ppy evidence add` and outputs inside the worktree",
            OUTSIDE_WORKTREE,
            program,
        )
    if refusal:
        from papaya_agent_runtime.capability_requests import ARGUMENTS

        return Verdict(suggestion, False, refusal, program=program, reach=ARGUMENTS)
    where = f"a versioned variant of {base}, in the safe family" if base else "in the safe family"
    return Verdict(suggestion, True, f"{program} is {where} ({family})", program=program)


def _path_verdict(program: str, name: str, worktree: str | None, roots: Iterable[str]) -> Verdict:
    """A program named by path: asked for as its basename, granted as the path it ran by."""
    from papaya_agent_runtime.capability_requests import IN_WORKTREE, pattern_for

    reach, resolved = path_reach(program, worktree, roots)
    try:
        pattern = pattern_for(name, path=program)
    except Exception:  # noqa: BLE001 - a path no pattern can hold is said, not requested
        return Verdict("", False, f"`{program}` cannot be written as a tool pattern", program=name)
    where = "inside the worktree" if reach == IN_WORKTREE else f"{reach} ({resolved})"
    return Verdict(
        pattern,
        False,
        f"{name} named by path, {where}: asked for as `{pattern}`",
        program=name,
        path=program,
        reach=reach,
        resolved=resolved,
    )


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
    # The LAST lines. A failing `make verify` opens with "make verify failed" and
    # then prints forty lines of log; what actually broke is at the end of it.
    lines = [line for line in text.splitlines() if line.strip()]
    kept = lines[-HOOK_SAID_LINES:]
    # Keep the opening line too when it was cut: it names the hook's own verdict.
    if len(lines) > HOOK_SAID_LINES and lines[0] not in kept:
        kept = [lines[0], "…", *kept[2:]]
    return "\n".join(kept)


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


def _repository_roots(conn, task_id: int | None) -> tuple[str, ...]:
    """The task's repository base clone: where its worktree's linked virtualenv lives."""
    if task_id is None:
        return ()
    row = conn.execute(
        "SELECT r.local_path FROM tasks t JOIN repos r ON r.id = t.repo_id WHERE t.id = ?",
        (task_id,),
    ).fetchone()
    return (str(row["local_path"]),) if row is not None and row["local_path"] else ()


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
        roots = _repository_roots(conn, task_id)
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
            verdict = classify(tool, command, worktree, refusal=refusal, roots=roots)
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
            if verdict.path or verdict.reach:
                payload.update(path=verdict.path, reach=verdict.reach, resolved=verdict.resolved)
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
            _steer_about(task_id, run_id, new, branch, worktree)
            # The request comes first: a denial that entered the loop is the manager's
            # (or, escalated, the connection owner's), never a GitHub issue.
            _request_capabilities(task_id, new, profile)
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
    if not any(p["in_family"] for p in new):
        return []
    return config_changes.apply(context=f"learned from task {task_id}'s denials")


def _request_capabilities(task_id: int, new: list[dict], profile: set[str] | None = None) -> None:
    """A gap in the profile that learning cannot close is a request. Never raises.

    The worker does not have to ask twice: the refusal itself is the need, decided by
    this install's policy and, when policy cannot, put in front of the manager
    (`capability_requests`) — a program, a program named by path, or a tool that is
    not the shell, on any stack. Not a pattern the profile already has: a grant of
    it would change nothing, so that denial stays the runtime's own to report.

    Each payload is marked with the outcome, ``request_id`` or ``request_error``, so the
    ledger can tell a denial the loop carries from one it could not record.
    """
    from papaya_agent_runtime import capability_requests

    for payload in new:
        program = str(payload.get("program") or "")
        pattern = str(payload.get("pattern") or "")
        if payload.get("kind") != PROFILE_GAP or payload.get("in_family") or not program:
            continue
        if not pattern or (profile is not None and pattern in profile):
            continue
        tool = str(payload.get("tool") or "Bash")
        try:
            made = capability_requests.request(
                task_id,
                program,
                why="",
                source=capability_requests.DENIAL,
                command=payload.get("command") or (None if tool == "Bash" else tool),
                path=payload.get("path") or None,
                reach=payload.get("reach") or None,
                resolved=payload.get("resolved") or None,
            )
            payload["request_id"] = made.id
        except Exception as exc:  # noqa: BLE001 - a request that cannot be made is logged
            payload["request_error"] = str(exc) or type(exc).__name__
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
        "request_id": payload.get("request_id"),
        "request_error": payload.get("request_error"),
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


def shape_steer_message(
    commands: list[str],
    branch: str | None,
    worktree: str | None = None,
    task_id: int | str = "<task id>",
) -> str:
    """The exact command to run instead of each refused one, then the rules it broke.

    The rules alone were what this steer used to say, and workers kept writing the
    same shapes back (issue #121: 30 occurrences, 16 of them `cd <worktree> && …`).
    A worker that has just been refused needs ONE command it can run, with its own
    directory in it — not three rows about `cd` in general — so `rewrite_for` builds
    it from the refused text and the worktree, and the rules come after.
    """
    from papaya_agent_runtime.providers.command_rules import command_rules, rewrites_for

    rewrites = rewrites_for(commands, worktree, task_id)
    unmatched = [c for c in commands if all(r.command != c for r in rewrites)]
    said = [
        "These shell commands were refused for their shape, not for the program they ran, "
        "and the runtime will not add a tool for them.",
    ]
    if rewrites:
        said.append(
            "Run this instead — your shell already starts in your worktree, so there is "
            "nothing to `cd` into to reach your own files:\n\n"
            + "\n".join(r.line() for r in rewrites)
        )
    if unmatched:
        said.append(
            "No single replacement fits these; split them into one plain command per "
            "call:\n\n" + "\n".join(f"- `{c}`" for c in unmatched)
        )
    said.append(
        "Every command you run is held to these rules:\n\n" + command_rules("claude", branch)
    )
    return "\n\n".join(said)


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


def _steer_about(
    task_id: int,
    run_id: int | None,
    new: list[dict],
    branch: str | None,
    worktree: str | None = None,
) -> None:
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
            steers.append(shape_steer_message(shapes, branch, worktree, task_id))
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
        from papaya_agent_runtime.providers.command_rules import rewrite_for

        # The row that MATCHED, so the tally and the steer name the same shape. It
        # used to take the first of several generic rows, which counted a
        # `cd <sub> && git …` as a plain `cd` and hid which rewrite was not landing.
        found = rewrite_for(str(payload.get("command") or ""), payload.get("worktree"))
        return found.shape if found is not None else "other"

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
    "DEFAULT_SAFE_FAMILY",
    "FAMILY_KINDS",
    "Verdict",
    "capability_policy",
    "classify",
    "family_kind",
    "safe_family",
    "variant_of",
    "counts",
    "hook_steer_message",
    "kind_of",
    "learn",
    "learnable",
    "record",
    "refused",
    "steer_worker",
]
