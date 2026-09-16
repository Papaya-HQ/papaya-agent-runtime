"""The prompts `ppy serve` gives its manager turns, as reviewed text.

Four turns, four files beside this one: ``brief.md`` (choose the repository, define
done on the record, brief and dispatch), ``answer.md`` (unblock a worker's question
or take it to a person), ``review.md`` (review at head, deliver, report back) and
``checkin.md`` (the rounds' look at a running worker: continue, steer, or stop and
resume, said on one last line the runner acts on).

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
TURNS = (BRIEF, ANSWER, REVIEW, CHECKIN)

#: The skill files every turn prompt names, relative to the runtime directory.
BRIEF_SKILL = ".agents/skills/brief-a-worker/SKILL.md"
REVIEW_SKILL = ".agents/skills/review-a-worker/SKILL.md"

#: The one placeholder a prompt file may contain.
RUNTIME_DIR = "{runtime_dir}"

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
    "and its scoped gate is green, and in any case before starting a run that may exceed ten "
    "minutes (a full suite, `make verify`, a build). The commit message says which goal; the "
    "push goes to your task's lease branch."
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

#: How a check-in turn says what it decided: its last line starts with this, then
#: one of the three decisions below (the steer and stop ones carry the message).
CHECKIN_PREFIX = "CHECK-IN:"
CHECKIN_CONTINUE = "continue"
CHECKIN_STEER = "steer"
CHECKIN_STOP = "stop and resume with"
CHECKIN_DECISIONS = (CHECKIN_CONTINUE, CHECKIN_STEER, CHECKIN_STOP)

_HERE = Path(__file__).resolve().parent


def path(turn: str) -> Path:
    """Where ``turn``'s prompt lives."""
    if turn not in TURNS:
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
    lines = ["", "## This ticket", ""]
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
    "CHECKIN_PREFIX",
    "CHECKIN_STEER",
    "CHECKIN_STOP",
    "REVIEW",
    "PUSH_MILESTONE_RULE",
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
