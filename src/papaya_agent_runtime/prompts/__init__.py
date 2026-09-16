"""The prompts `ppy serve` gives its manager turns, as reviewed text.

Three turns, three files beside this one: ``brief.md`` (choose the repository, define
done on the record, brief and dispatch), ``answer.md`` (unblock a worker's question
or take it to a person) and ``review.md`` (review at head, deliver, report back).

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
TURNS = (BRIEF, ANSWER, REVIEW)

#: The skill files every turn prompt names, relative to the runtime directory.
BRIEF_SKILL = ".agents/skills/brief-a-worker/SKILL.md"
REVIEW_SKILL = ".agents/skills/review-a-worker/SKILL.md"

#: The one placeholder a prompt file may contain.
RUNTIME_DIR = "{runtime_dir}"

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
    "ANSWER",
    "BRIEF",
    "BRIEF_SKILL",
    "REVIEW",
    "REVIEW_SKILL",
    "RUNTIME_DIR",
    "TURNS",
    "load",
    "path",
    "render",
]
