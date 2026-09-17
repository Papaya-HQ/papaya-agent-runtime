"""The prompts `ppy serve` gives its manager turns, as reviewed text.

Five turns, five files beside this one: ``brief.md`` (choose the repository, define
done on the record, brief and dispatch), ``answer.md`` (unblock a worker's question
or take it to a person), ``review.md`` (review at head, deliver, report back),
``checkin.md`` (the rounds' look at a running worker: continue, steer, or stop and
resume, said on one last line the runner acts on) and ``ledger.md`` (the next steps
that sat in the ledger: do each, defer it with a reason, or drop it). The answer and
review turns run with a held Papaya work item or without one (a worker dispatched from
a session, or whose ticket ended: `lanes`); the facts say which.

They instruct; they do not template. Task 222 was closed for generating briefs in
code, and the rule that came out of it is kept here structurally: the only thing
substituted into a prompt is where this runtime lives, so the skill files can be
named by absolute path. What the turn is *about* is appended after the prompt as a
short list of facts — ids, the repository if one is known, a worker's question or
failure, verbatim — and never as a Goals sentence, a definition of done or a
fill-in brief. Writing those is the turn's job.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

BRIEF = "brief"
ANSWER = "answer"
REVIEW = "review"
CHECKIN = "checkin"
LEDGER = "ledger"
TURNS = (BRIEF, ANSWER, REVIEW, CHECKIN, LEDGER)
#: Not a manager turn: the scoped brief a reconciler worker starts from when the
#: session that delivered a pull request cannot be resumed to fix it.
RECONCILE = "reconcile"

#: The skill files every turn prompt names, relative to the runtime directory.
BRIEF_SKILL = ".agents/skills/brief-a-worker/SKILL.md"
REVIEW_SKILL = ".agents/skills/review-a-worker/SKILL.md"

#: The one placeholder a prompt file may contain.
RUNTIME_DIR = "{runtime_dir}"

#: The heading the facts are appended under. Not "this ticket": a turn keyed on a task
#: (`lanes`) holds no ticket, and its facts say so.
FACTS_HEADING = "## The facts"

#: What a rerun is told when the runner found a turn's obligation unmet on the
#: record. One line each, appended as a fact; the prompts themselves are unchanged.
ADDENDUM_FACT = "the runner's note"
ACCEPTANCE_ADDENDUM = (
    "A worker is already dispatched for this ticket, so do not dispatch again: the work "
    "item still has no acceptance criteria, so write them onto it from your brief's Goals "
    "through the papaya MCP server, say so in a comment, then end."
)
REPORT_ADDENDUM = (
    "The pull request is already delivered, so do not review or deliver again: post the "
    "result on the work item through the papaya MCP server, then end."
)

#: How a brief or review turn says it ended on purpose, still waiting on something
#: (a gate that outlasts the turn): the first line of its last message starts with
#: this. The runner reruns such a turn later instead of counting it as a miss.
WAITING_PREFIX = "WAITING:"

#: The rule every turn and worker is given about gates longer than a tool call. A
#: harness caps a tool call at ten minutes and backgrounds anything longer, and a
#: backgrounded command dies with the session (PAP-213). The prompt files carry it
#: verbatim; a test holds them to it.
TEN_MINUTE_RULE = (
    "A command that may run longer than ten minutes must not be run as a tool call; "
    "use `ppy gate run`, or push and let the hook run it. Never background a gate and wait."
)

#: The rule every brief, environment block and command-rules block gives a worker
#: about when its work reaches the remote. A worker once held two hours of finished
#: work in its worktree for one commit after a green full suite, and a restart would
#: have stranded all of it (PAP-219). Carried verbatim next to the ten-minute rule;
#: a test holds all three to it.
PUSH_MILESTONE_RULE = (
    "Commit and push at milestones, not once at the end: after each goal in the brief lands "
    "and its scoped gate is green (the scoped gate, never the full suite), and in any case "
    "before starting a run that may exceed ten minutes (a build, a long scoped gate). The "
    "commit message says which goal; the push goes to your task's lease branch."
)

#: The three tiers of checking, named the same way in every worker-facing text: the
#: brief, the environment block, the command rules and the review prompt. On 2026-09-17
#: a backend's recorded local gate was its sixteen-minute full suite, so every milestone,
#: every hand-back and the review ran it: six runs over two tickets. Which commands fill
#: each tier is the repository's to say (its AGENTS.md, CONTRIBUTING, Makefile, CI); the
#: tiers themselves are the runtime's rule, and a test holds every text to this one.
GATE_TIERS_RULE = (
    "Check in three tiers. Targeted checks while you work: the tests nearest your change, "
    "chosen from the repository's own guidance and your diff, as often as you like. The "
    "scoped gate before you hand back: the quick gate the repository names, never the full "
    "suite. The full suite once, at the head that will be delivered: run by the supervisor "
    "(`ppy gate run --full`) or by CI when CI runs it; never by a worker as a tool call, "
    "and never at a milestone."
)

#: How any turn says the runtime itself, not the repository, got in its way: a line
#: starting with this at the end of its last message. The runner records it as a
#: deficiency (`deficiencies.py`) and `ppy serve` opens an issue about it.
RUNTIME_PREFIX = "RUNTIME:"

#: The rule every turn prompt carries verbatim, and a test holds them to.
RUNTIME_RULE = (
    "If the runtime itself got in the way of this turn (not the repository and not the "
    "work, but a tool you were refused, a fact you could not obtain, or a contract such as "
    "these instructions or a `ppy` command's output that was not true), say so in one line "
    "of its own: `RUNTIME: <what got in the way>`. Name ids, never people, and quote no code "
    "or ticket text. Leave it out when nothing did."
)

#: The rule every turn prompt carries verbatim about where a durable fact goes. On a
#: shared agent Papaya refuses a machine-extracted memory, and turns that were told to
#: propose one anyway hit the refusal every time and reported it twice (issues #40, #49).
#: The runner puts `agent:` and `memory:` in every turn's facts; a test holds the four
#: prompts to this text.
MEMORY_RULE = (
    "Where a durable fact goes depends on this agent, which the facts at the end of this "
    "prompt name as `agent:` and `memory:`. On a shared agent (`memory: repo-notes-only`), "
    "durable facts go to the repository's memory notes, `.ppy/memory/repos/<repo>/notes.md` "
    "in this runtime directory, never to `propose_memory`: Papaya refuses a machine-extracted "
    "memory on a shared agent, because the whole workspace would see it. Only with "
    "`memory: papaya` may you also propose a memory under your identity."
)

#: The rule every brief, environment block and command-rules block gives a worker about
#: the pull request its work becomes. Three PRs in one afternoon went conflicting or fell
#: behind a base that required up-to-date branches after their workers had moved on, and
#: no worker knew any of it was still theirs (PRs 27, 28, 29). Carried verbatim beside
#: the ten-minute and push-milestone rules; a test holds all three to it.
PR_FOLLOW_RULE = (
    "Your pull request is yours until it merges: after delivery you will be steered back "
    "for red CI, merge conflicts, a branch behind its base, or reviewer comments. Fix it on "
    "the same branch, never open a second pull request, and never force-push over a "
    "reviewer's view without saying so in the pull request."
)

#: How a check-in turn says what it decided: its last line starts with this, then
#: one of the three decisions below (the steer and stop ones carry the message; a
#: continue may carry a note, `continue, note <text>`, which is never a steer).
CHECKIN_PREFIX = "CHECK-IN:"
CHECKIN_CONTINUE = "continue"
CHECKIN_NOTE = "note"
CHECKIN_STEER = "steer"
CHECKIN_STOP = "stop and resume with"
CHECKIN_DECISIONS = (CHECKIN_CONTINUE, CHECKIN_STEER, CHECKIN_STOP)

_HERE = Path(__file__).resolve().parent


def path(turn: str) -> Path:
    """Where ``turn``'s prompt lives."""
    if turn not in (*TURNS, RECONCILE):
        raise ValueError(f"unknown turn {turn!r}; expected one of {', '.join(TURNS)}")
    return _HERE / f"{turn}.md"


def load(turn: str) -> str:
    """``turn``'s prompt exactly as reviewed, placeholder and all."""
    return path(turn).read_text(encoding="utf-8")


def render(turn: str, *, runtime_dir: str | Path, facts: Mapping[str, object]) -> str:
    """The prompt for one launch: paths resolved, then the facts, then nothing else.

    ``facts`` are rendered as a plain list in the order given. A value that is
    ``None`` or empty is left out rather than written as "none", so a turn never
    reads an absent repository as a repository called "none". Multi-line values
    (a worker's question, a failure summary) are kept verbatim in a fenced block.
    """
    body = load(turn).replace(RUNTIME_DIR, str(Path(runtime_dir).resolve()))
    lines = ["", FACTS_HEADING, ""]
    blocks: list[str] = []
    for key, value in facts.items():
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        if "\n" in text:
            blocks.extend(["", f"{key}:", "", "```", text, "```"])
        else:
            lines.append(f"- {key}: {text}")
    return body.rstrip() + "\n" + "\n".join(lines + blocks) + "\n"


__all__ = [
    "ACCEPTANCE_ADDENDUM",
    "ADDENDUM_FACT",
    "ANSWER",
    "REPORT_ADDENDUM",
    "BRIEF",
    "BRIEF_SKILL",
    "CHECKIN",
    "CHECKIN_CONTINUE",
    "CHECKIN_DECISIONS",
    "CHECKIN_NOTE",
    "CHECKIN_PREFIX",
    "CHECKIN_STEER",
    "CHECKIN_STOP",
    "FACTS_HEADING",
    "GATE_TIERS_RULE",
    "LEDGER",
    "MEMORY_RULE",
    "REVIEW",
    "PR_FOLLOW_RULE",
    "PUSH_MILESTONE_RULE",
    "RECONCILE",
    "REVIEW_SKILL",
    "RUNTIME_DIR",
    "RUNTIME_PREFIX",
    "RUNTIME_RULE",
    "TEN_MINUTE_RULE",
    "TURNS",
    "WAITING_PREFIX",
    "load",
    "path",
    "render",
]
