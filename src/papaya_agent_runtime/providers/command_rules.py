"""Provider-specific command rules, prepended to a fresh worker's prompt.

A Claude worker runs under an explicit tool allowlist, and the allowlist matches
*one plain command per call*. Compound shell — pipes, ``&&``, ``;``, an inline
env assignment, a redirection — does not match any pattern and is denied; ``cd``
has to be its own call; and there is no ``gh``, so the worker cannot open a pull
request even if it wanted to.

Every brief was carrying a hand-written "Worker command rules (Claude provider)"
section to say all of that. Hand-copied rules drift and get forgotten, and a
worker that hits a denial with nowhere to record it either retries blindly or
goes quiet. So the rules live here, in one place, and the runtime prepends them
at dispatch. Codex workers run in their own sandbox with a real shell and get
nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from papaya_agent_runtime import prompts

HEADING = "Command rules for this environment"

#: What a worker does with a refused command. The rules end with it, and a steer about a
#: refusal (`tool_learning`) quotes it rather than saying it a second way.
FLAGGED_RULE = """\
If a command is denied, do not retry it in a different shape and do not silently
skip the work it was for. Record it verbatim in your final report under a
heading:

## Flagged, not done

- `<the exact command that was denied>` — what it was for, and what it means for
  the task.
"""

#: What a worker does instead of pushing, where the repository gates pushes. Said in
#: the rules at dispatch and again in the steer when a hook refuses a push, so the
#: worker never reads the refusal as something to retry or route around.
RUNTIME_PUSHES_RULE = (
    "**Do not push in this repository; the runtime pushes for you.** Commit your work "
    "and report done. The runtime runs its own gate at your exact head and, when that "
    "is green, pushes your lease branch itself — outside the harness, so the "
    "repository's hook is neither triggered nor weakened. Never use `--no-verify`, "
    "never force, and never edit, skip or disable a hook to get a push through."
)

#: Long or punctuation-heavy text is refused as a *command*, whatever the program:
#: Claude Code will not statically analyse a quoted argument holding a newline before
#: a `#`, a backtick, `$(`, or a brace with a quote in it. Issue #115 is nothing but
#: this. Every refused note on record carries one of those; no plain one-line note
#: has ever been refused.
NOTE_FILE_RULE = (
    "**Write long notes to a file and pass the file.** A note, a reason or a commit "
    "message with more than one line — or holding backticks, `$`, `#`, braces or "
    "quotes — is refused as a command however plain the program is, because the "
    "harness will not analyse the argument. Do not reshape the text and do not use "
    "`$(cat …)`, which is command substitution and refused too. Write the text with "
    "the file-writing tool, then pass the path: `ppy progress <task id> --phase "
    "<phase> --note-file <path>`, `ppy need <task id> --capability <program> "
    "--why-file <path>`, `git commit -F <path>`. The file belongs in your worktree or "
    "its evidence directory. Short single-line notes still work inline with `--note`."
)

#: The rewrite for each shape workers actually get refused on, measured from the
#: runtime's own record. A steer about a shape denial carries the rows that apply, so
#: the worker is given the command to run rather than the rule it broke again.
REWRITES: tuple[tuple[str, str], ...] = (
    ("cd <your worktree> && <command>", "`<command>` — your shell already starts there"),
    ("cd <worktree>/<subdir> && git <args>", "`git -C <subdir> <args>`"),
    (
        "cd <worktree>/<subdir> && <command>",
        "the command's own directory flag (`uv run --directory <subdir> …`, "
        "`make -C <subdir> …`, `pnpm --dir <subdir> …`)",
    ),
    (
        "<command> | head -<n>, | tail -<n>, | less",
        "the Read tool on the file, or the command's own flag",
    ),
    ("<command> | grep <pattern>", "the Grep tool, or the command's own `--filter`/`-k` flag"),
    ("<command> > <file>, >> <file>, 2>&1", "the file-writing tool; for a note, `--note-file`"),
    ("<command> 2>&1 | tee <file> | tail -<n>", "`ppy gate run`, then the Read tool on its output"),
    ("cat >> <file> <<'EOF' … EOF", "the file-writing or file-editing tool"),
    ("FOO=1 <command>", "`ppy need <task id> --capability <program> --why-file <path>`"),
    ("for x in a b c; do <command>; done", "one call per value — run it three times"),
    ("<command a>; <command b>", "two calls, one command each"),
    ('git commit -m "$(cat <file>)"', "`git commit -F <file>`"),
    ('ppy progress <id> --note "<long text>"', "`ppy progress <id> --note-file <path>`"),
)


def rewrite_table(rows: tuple[tuple[str, str], ...] = REWRITES) -> str:
    """The rewrites as a markdown list: what was refused, and what to run instead."""
    return "\n".join(f"- `{refused}` -> {instead}" for refused, instead in rows)


#: Programs whose own flag names a directory, so `cd X && prog …` has a real rewrite.
DIRECTORY_FLAGS: dict[str, str] = {
    "git": "git -C {dir} {rest}",
    "make": "make -C {dir} {rest}",
    "uv": "uv run --directory {dir} {rest}",
    "pnpm": "pnpm --dir {dir} {rest}",
    "npm": "npm --prefix {dir} {rest}",
    "pytest": "pytest --rootdir {dir} {rest}",
}


@dataclass(frozen=True)
class Rewrite:
    """One refused command and the ONE thing to run instead of it."""

    #: The refused command, verbatim.
    command: str
    #: Which :data:`REWRITES` shape it is — what :func:`tool_learning.shape_counts`
    #: buckets on, so the tally and the advice always name the same thing.
    shape: str
    #: The exact replacement, or a sentence saying no replacement exists.
    instead: str
    #: False when there is nothing to run instead and the worker must stop and say so.
    runnable: bool = True

    def line(self) -> str:
        arrow = "->" if self.runnable else "— "
        return f"- `{self.command}`\n  {arrow} {self.instead}"


def rewrite_for(command: str, worktree: str | None = None) -> Rewrite | None:
    """The one exact command to run instead of ``command``, or None if it is not a shape.

    Not a list of generic rows. A worker that has just been refused
    `cd /path/to/wt/backend && git status` needs `git -C backend status`, with the
    real directory in it — three rows about `cd` in general are what it had before,
    and it kept writing the same command back (issue #121, 30 occurrences).

    ``worktree`` is what makes the `cd` answer exact: the same text is a different
    rewrite depending on whether the directory IS the worktree, is inside it, or is
    somewhere the worker cannot reach at all.
    """
    for rule in (
        _rewrite_cd,
        _rewrite_inline_env,
        _rewrite_commit_substitution,
        _rewrite_note,
        _rewrite_heredoc,
        _rewrite_redirect,
        _rewrite_tee,
        _rewrite_pipe,
        _rewrite_loop,
        _rewrite_sequence,
    ):
        found = rule(command.strip(), worktree)
        if found is not None:
            return found
    return None


def rewrites_for(commands: list[str], worktree: str | None = None) -> list[Rewrite]:
    """One :class:`Rewrite` per command that is a known shape, in order, deduplicated."""
    found: list[Rewrite] = []
    seen: set[str] = set()
    for command in commands:
        rewrite = rewrite_for(command, worktree)
        if rewrite is not None and rewrite.command not in seen:
            seen.add(rewrite.command)
            found.append(rewrite)
    return found


def _split_cd(command: str) -> tuple[str, str] | None:
    """`cd X && rest` / `cd X; rest` -> (X, rest)."""
    found = re.match(
        r"^cd\s+(?P<dir>'[^']*'|\"[^\"]*\"|\S+)\s*(?:&&|;)\s*(?P<rest>.+)$", command, re.S
    )
    if found is None:
        return None
    return found.group("dir").strip("'\""), found.group("rest").strip()


def _where(target: str, worktree: str | None) -> tuple[str, str | None]:
    """Is ``target`` the worktree, inside it, or out of reach? With the relative path."""
    if not worktree:
        return "unknown", None
    root = Path(worktree).expanduser()
    path = Path(target).expanduser()
    if not path.is_absolute():
        path = root / path
    try:
        root_resolved = root.resolve()
        resolved = path.resolve()
    except OSError:
        return "unknown", None
    if resolved == root_resolved:
        return "worktree", None
    try:
        return "inside", str(resolved.relative_to(root_resolved))
    except ValueError:
        return "outside", None


def _rewrite_cd(command: str, worktree: str | None) -> Rewrite | None:
    parts = _split_cd(command)
    if parts is None:
        return None if not command.startswith("cd ") else _bare_cd(command, worktree)
    target, rest = parts
    place, relative = _where(target, worktree)
    if place == "worktree":
        return Rewrite(command, REWRITES[0][0], f"`{rest}`, on its own — your shell starts there")
    if place == "inside" and relative:
        program = (rest.split() or [""])[0]
        template = DIRECTORY_FLAGS.get(program)
        if template:
            tail = rest[len(program) :].strip()
            shape = REWRITES[1][0] if program == "git" else REWRITES[2][0]
            return Rewrite(command, shape, f"`{template.format(dir=relative, rest=tail)}`")
        return Rewrite(
            command,
            REWRITES[2][0],
            f"`{rest}` with that program's own directory flag pointed at `{relative}`; "
            f"`{program}` has none here, so run it from the worktree with `{relative}/` "
            "in its paths",
        )
    if place == "outside":
        return Rewrite(
            command,
            REWRITES[2][0],
            f"`{target}` is outside your worktree and nothing can reach it — not this "
            "command and not a rewrite of it. Record it under 'Flagged, not done'.",
            runnable=False,
        )
    return Rewrite(command, REWRITES[0][0], f"`{rest}`, on its own — your shell starts there")


def _bare_cd(command: str, worktree: str | None) -> Rewrite | None:
    """A `cd` with no second command: refused only when it leaves the worktree."""
    target = command[3:].strip().strip("'\"")
    if not target:
        return None
    place, _ = _where(target, worktree)
    if place == "outside":
        return Rewrite(
            command,
            REWRITES[2][0],
            f"`{target}` is outside your worktree and cannot be reached.",
            runnable=False,
        )
    return None


def _rewrite_inline_env(command: str, worktree: str | None) -> Rewrite | None:
    found = re.match(r"^(?P<assign>(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+)(?P<rest>.+)$", command, re.S)
    if found is None:
        return None
    rest = found.group("rest").strip()
    names = [a.split("=", 1)[0] for a in found.group("assign").split()]
    shape = REWRITES[8][0]
    # Some of these have a real alternative, and saying `ppy need` for them is wrong:
    # `ppy need` asks for a PROGRAM, and the program is already allowed.
    if names == ["GIT_EDITOR"] and rest.startswith("git "):
        return Rewrite(command, shape, f"`git -c core.editor=true {rest[4:].strip()}`")
    if set(names) <= {"UV_CACHE_DIR", "RUFF_CACHE_DIR", "MYPY_CACHE_DIR"}:
        return Rewrite(
            command,
            shape,
            f"`{rest}` — those cache variables are already set in your environment by "
            "the runtime, so setting them again does nothing",
        )
    return Rewrite(
        command,
        shape,
        f"nothing sets an environment variable for one call here. If `{rest}` cannot run "
        f"without `{', '.join(names)}`, say so in your report — and if what it needs is a "
        f"PROGRAM, `ppy need <task id> --capability <program> --why-file <path>`",
        runnable=False,
    )


def _rewrite_commit_substitution(command: str, worktree: str | None) -> Rewrite | None:
    if "git commit" not in command or "$(" not in command:
        return None
    found = re.search(r"\$\(\s*cat\s+(?P<file>[^\s)]+)", command)
    where = found.group("file") if found else "<path>"
    return Rewrite(command, REWRITES[11][0], f"`git commit -F {where}`")


def _rewrite_note(command: str, worktree: str | None) -> Rewrite | None:
    if not re.match(r"^(?:\./bin/)?ppy\s", command):
        return None
    found = re.search(r"--(?P<flag>note|why|self|manager)\b", command)
    if found is None:
        return None
    flag = found.group("flag")
    if flag in ("note", "why"):
        head = command[: found.start()].rstrip()
        return Rewrite(
            command,
            REWRITES[12][0],
            f"write the text with the file-writing tool, then `{head} --{flag}-file <path>`",
        )
    return Rewrite(
        command,
        REWRITES[12][0],
        f"`--{flag}` has no file form; keep that text to one line with no backticks, "
        "`$`, `#` or braces",
        runnable=False,
    )


def _rewrite_heredoc(command: str, worktree: str | None) -> Rewrite | None:
    if "<<" not in command:
        return None
    found = re.search(r"(?:>>|>)\s*(?P<file>[^\s<]+)", command)
    where = found.group("file").strip() if found else "the file"
    return Rewrite(
        command,
        REWRITES[7][0],
        f"write `{where}` with the file-writing tool (or the file-editing tool to append)",
    )


def _rewrite_redirect(command: str, worktree: str | None) -> Rewrite | None:
    if "|" in command or not re.search(r"(?<![0-9])>>?(?!&)", command):
        return None
    found = re.search(r"(?<![0-9])>>?\s*(?P<file>[^\s]+)", command)
    where = found.group("file") if found else "<file>"
    left = command[: found.start()].strip() if found else command
    return Rewrite(
        command,
        REWRITES[5][0],
        f"run `{left}`, read its output, and write `{where}` with the file-writing tool",
    )


def _rewrite_tee(command: str, worktree: str | None) -> Rewrite | None:
    if "tee" not in command or "|" not in command:
        return None
    left = command.split("|", 1)[0].strip()
    left = re.sub(r"\s*2>&1\s*$", "", left)
    return Rewrite(
        command,
        REWRITES[6][0],
        f"`ppy gate run --task <task id>` when `{left}` is your gate — it records the "
        "output for you; otherwise run it and read what it printed",
    )


def _rewrite_pipe(command: str, worktree: str | None) -> Rewrite | None:
    if "|" not in command:
        return None
    left, right = (part.strip() for part in command.split("|", 1))
    right_program = (right.split() or [""])[0]
    if right_program == "grep":
        pattern = re.sub(r"^grep\s+(?:-\S+\s+)*", "", right).strip()
        source = _file_argument(left)
        if source:
            return Rewrite(command, REWRITES[4][0], f"the Grep tool for {pattern} in `{source}`")
        return Rewrite(command, REWRITES[4][0], f"run `{left}` and read its output for {pattern}")
    if right_program in ("head", "tail", "less", "more"):
        source = _file_argument(left)
        if source:
            return Rewrite(command, REWRITES[3][0], f"the Read tool on `{source}`")
        return Rewrite(command, REWRITES[3][0], f"run `{left}` and read what it printed")
    return Rewrite(
        command,
        REWRITES[3][0],
        f"run `{left}` on its own and do `{right_program}`'s part by reading the output",
    )


def _file_argument(command: str) -> str | None:
    """The file a plain reader was pointed at, when that is all the command is."""
    words = command.split()
    if not words or words[0] not in ("cat", "wc", "head", "tail", "sort", "uniq"):
        return None
    paths = [w for w in words[1:] if not w.startswith("-") and not w.startswith("2>")]
    return paths[0] if paths else None


def _rewrite_loop(command: str, worktree: str | None) -> Rewrite | None:
    found = re.match(
        r"^for\s+(?P<var>\w+)\s+in\s+(?P<values>.+?);\s*do\s+(?P<body>.+?);?\s*done\s*$",
        command,
        re.S,
    )
    if found is None:
        return None
    values = found.group("values").split()
    body = found.group("body").strip()
    without = re.sub(r"\$\{?" + re.escape(found.group("var")) + r"\}?", "<value>", body)
    return Rewrite(
        command,
        REWRITES[9][0],
        f"`{without}`, once per value — {len(values)} separate calls",
    )


def _rewrite_sequence(command: str, worktree: str | None) -> Rewrite | None:
    if ";" not in command and "&&" not in command:
        return None
    parts = [p.strip() for p in re.split(r"&&|;", command) if p.strip()]
    if len(parts) < 2:
        return None
    return Rewrite(
        command,
        REWRITES[10][0],
        "one call each: " + ", then ".join(f"`{p}`" for p in parts),
    )


_CLAUDE_RULES = (
    """\
## Command rules for this environment

Your shell runs under an allowlist that matches **one plain command per call**.
These are not style preferences — anything else is denied before it runs.

- **Your shell already starts in your worktree.** There is nothing to `cd` into
  to reach your own files: run the command on its own. For a subdirectory, use the
  command's own directory flag (`git -C <subdir>`, `make -C <subdir>`,
  `uv run --directory <subdir>`) rather than changing directory first.
- One command per call. No pipes (`|`), no `&&`, no `;`, no command substitution.
  If you need the output of one command in another, run them separately and read
  the output yourself.
- No inline environment assignments (`FOO=1 cmd`). Export is not available
  either; if a command needs an environment variable, say so in your report
  instead of working around it.
- No redirection (`>`, `>>`, `<`, `2>&1`). To write a file, use the file-writing
  tool, not a shell redirect.
- `cd` is its own call. Never prefix another command with it.
- {note_file_rule}
- {gate_tiers_rule}
- Run the scoped gate **in the foreground** —
  never as a background task. A backgrounded command is killed when your turn
  ends, so a gate you left running in the background never finished and its
  result is worthless. A tool call is capped at ten minutes and anything longer is moved to
  the background for you: {ten_minute_rule}
- {full_suite_refusal}
- {push_milestone_rule}
- {pr_follow_rule}
- {push_rule}
- **Do not try to open a pull request.** You have no `gh` and no forge
  credentials. {after_push} the manager opens the PR from it.
- **Ask for a program before you need it.** In your plan phase, for every program
  the work needs beyond reading, editing and your repository's own gate (a code
  generator, a database client, a browser driver), run `ppy need <task id>
  --capability <program> --why "<what for>"`. It is granted by this machine's policy
  or put to a person, and the output says which; a grant reaches you when the runtime
  resumes you. A refused command is turned into the same request for you.

Refused a command for its shape? This is what to run instead:

{rewrite_table}

"""
    + FLAGGED_RULE
)

#: Why a worker's full-suite tool call is refused: said in the rules before it happens,
#: and again in the steer after it does (`tool_learning.policy_rule`).
FULL_SUITE_REFUSAL = (
    "The repository's full suite is refused as a tool call: it runs once, at the head that "
    "will be delivered, under the supervisor or in CI. Your last check before handing back "
    "is the scoped gate."
)

#: The program name a full-suite denial is recorded under (`tool_learning`).
FULL_SUITE_PROGRAM = "full suite"


def denied_tools(full_suite_command: str | None) -> list[str]:
    """The Claude patterns a worker is refused on top of its allowlist: the full suite.

    The exact command only. A prefix pattern would also refuse a targeted run that
    starts the same way (`uv run pytest tests/test_x.py` under `uv run pytest`).
    """
    command = " ".join((full_suite_command or "").split())
    return [f"Bash({command})"] if command else []


def command_rules(
    provider: str,
    branch: str | None = None,
    environment: str | None = None,
    *,
    runtime_pushes: bool = False,
) -> str:
    """The command-rules block for ``provider``, or "" when it needs none.

    Single source of truth: the dispatch path, the docs, and the tests all read
    this. ``branch`` fills in the push line; without one the worker is told to use
    its own branch name, which it can read from git.

    ``runtime_pushes`` is for a repository whose own hook gates pushes: the worker
    is told not to push at all, because the runtime pushes the lease branch itself
    after its gate. Telling a worker there to run the push is telling it to be
    refused (issues #83, #116) — fourteen times on the record.

    ``environment`` is the per-repository environment block dispatch rendered
    (:mod:`papaya_agent_runtime.environment`): the evidence directory, the local gate,
    the private database stack, the push-hook policy. It is appended after the
    command rules for a Claude worker and is the whole block for a Codex worker,
    whose sandbox has a real shell and needs no command rules — the environment
    facts are the repository's, not the provider's.
    """
    block = (environment or "").strip()
    if provider != "claude":
        return f"{block}\n" if block else ""
    rules = _CLAUDE_RULES.format(
        ten_minute_rule=prompts.TEN_MINUTE_RULE,
        gate_tiers_rule=prompts.GATE_TIERS_RULE,
        full_suite_refusal=FULL_SUITE_REFUSAL,
        push_milestone_rule=prompts.PUSH_MILESTONE_RULE,
        pr_follow_rule=prompts.PR_FOLLOW_RULE,
        note_file_rule=NOTE_FILE_RULE,
        rewrite_table=rewrite_table(),
        push_rule=(
            RUNTIME_PUSHES_RULE
            if runtime_pushes
            else f"Push with exactly `git push origin HEAD:{branch or '<your task branch>'}`."
        ),
        after_push=("Commit and report done;" if runtime_pushes else "Push your branch and stop;"),
    )
    return f"{rules}\n{block}\n" if block else rules
