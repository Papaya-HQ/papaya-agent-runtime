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

_CLAUDE_RULES = (
    """\
## Command rules for this environment

Your shell runs under an allowlist that matches **one plain command per call**.
These are not style preferences — anything else is denied before it runs.

- One command per call. No pipes (`|`), no `&&`, no `;`, no command substitution.
  If you need the output of one command in another, run them separately and read
  the output yourself.
- No inline environment assignments (`FOO=1 cmd`). Export is not available
  either; if a command needs an environment variable, say so in your report
  instead of working around it.
- No redirection (`>`, `>>`, `<`, `2>&1`). To write a file, use the file-writing
  tool, not a shell redirect.
- `cd` is its own call. Never prefix another command with it.
- {gate_tiers_rule}
- Run the scoped gate **in the foreground** —
  never as a background task. A backgrounded command is killed when your turn
  ends, so a gate you left running in the background never finished and its
  result is worthless. A tool call is capped at ten minutes and anything longer is moved to
  the background for you: {ten_minute_rule}
- {full_suite_refusal}
- {push_milestone_rule}
- {pr_follow_rule}
- Push with exactly `git push origin HEAD:{branch}`.
- **Do not try to open a pull request.** You have no `gh` and no forge
  credentials. Push your branch and stop; the manager opens the PR from it.
- **Ask for a program before you need it.** In your plan phase, for every program
  the work needs beyond reading, editing and your repository's own gate (a code
  generator, a database client, a browser driver), run `ppy need <task id>
  --capability <program> --why "<what for>"`. It is granted by this machine's policy
  or put to a person, and the output says which; a grant reaches you when the runtime
  resumes you. A refused command is turned into the same request for you.

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


def command_rules(provider: str, branch: str | None = None, environment: str | None = None) -> str:
    """The command-rules block for ``provider``, or "" when it needs none.

    Single source of truth: the dispatch path, the docs, and the tests all read
    this. ``branch`` fills in the push line; without one the worker is told to use
    its own branch name, which it can read from git.

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
        branch=branch or "<your task branch>",
        ten_minute_rule=prompts.TEN_MINUTE_RULE,
        gate_tiers_rule=prompts.GATE_TIERS_RULE,
        full_suite_refusal=FULL_SUITE_REFUSAL,
        push_milestone_rule=prompts.PUSH_MILESTONE_RULE,
        pr_follow_rule=prompts.PR_FOLLOW_RULE,
    )
    return f"{rules}\n{block}\n" if block else rules
