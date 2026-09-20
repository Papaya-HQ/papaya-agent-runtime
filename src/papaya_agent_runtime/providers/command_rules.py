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


def rewrites_for(commands: list[str]) -> tuple[tuple[str, str], ...]:
    """The rewrite rows that apply to ``commands``, or all of them when none match.

    A steer naming three shapes should not re-print thirteen rules; a steer about a
    shape nothing matches is still better off with the whole table than with none.
    """
    matched = tuple(row for row in REWRITES if any(_applies(row, c) for c in commands))
    return matched or REWRITES


def _applies(row: tuple[str, str], command: str) -> bool:
    refused = row[0]
    if refused.startswith("cd <worktree>/<subdir> && git"):
        return bool(re.search(r"^cd\s+\S+\s*&&\s*git\b", command))
    if refused.startswith("cd <worktree>/<subdir>"):
        return bool(re.search(r"^cd\s+\S+\s*&&", command))
    if refused.startswith("cd <your worktree>"):
        return command.startswith("cd ")
    if refused.startswith("git commit -m"):
        return "git commit" in command and "$(" in command
    if refused.startswith("ppy progress"):
        return command.startswith(("ppy ", "./bin/ppy ")) and "--note" in command
    if refused.startswith("cat >>"):
        return "<<" in command
    if refused.startswith("for x in"):
        return bool(re.match(r"^for\s+\w+\s+in\b", command))
    if refused.startswith("FOO=1"):
        return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", command))
    if "2>&1 | tee" in refused:
        return "tee" in command and "|" in command
    if "| head" in refused:
        return bool(re.search(r"\|\s*(head|tail|less)\b", command))
    if "| grep" in refused:
        return bool(re.search(r"\|\s*grep\b", command))
    if "> <file>" in refused:
        return bool(re.search(r"(?<![0-9])>>?[^&]|2>&1", command))
    if refused.startswith("<command a>"):
        return ";" in command
    return False


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
