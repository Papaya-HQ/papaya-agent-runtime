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

_CLAUDE_RULES = """\
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
- Run the authoritative verification suite **in the foreground** —
  never as a background task. A backgrounded command is killed when your turn
  ends, so a suite you left running in the background never finished and its
  result is worthless. A tool call is capped at ten minutes and anything longer is moved to
  the background for you: {ten_minute_rule}
- {push_milestone_rule}
- {pr_follow_rule}
- Push with exactly `git push origin HEAD:{branch}`.
- **Do not try to open a pull request.** You have no `gh` and no forge
  credentials. Push your branch and stop; the manager opens the PR from it.

If a command is denied, do not retry it in a different shape and do not silently
skip the work it was for. Record it verbatim in your final report under a
heading:

## Flagged, not done

- `<the exact command that was denied>` — what it was for, and what it means for
  the task.
"""


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
        push_milestone_rule=prompts.PUSH_MILESTONE_RULE,
        pr_follow_rule=prompts.PR_FOLLOW_RULE,
    )
    return f"{rules}\n{block}\n" if block else rules
