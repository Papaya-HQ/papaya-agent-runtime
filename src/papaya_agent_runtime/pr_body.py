"""The pull request body a delivery writes: a description a person wrote for people.

Every delivery on 2026-09-04 opened its pull request with the body "Automated
delivery of task N (sha)." The fix then was to compose the body from what the run
already held — the brief's first section, the worker's closing report, the reviewer's
approval note — quoted verbatim, "nothing invented".

That produced bodies nobody outside the run could read (Shane, 2026-09-21, on
papaya-frontend-monorepo PR #811: "cryptic slop"). Every source was written for the
machine: a worker's last progress note is the story of its final round ("DONE
(evidence round). New head … Not pushed by me"), a brief's first section is often
repository layout, and a reviewer's note is shorthand for the ledger. None of them
says what changed for a user or how a person should check it, and a quote cannot say
what its author never wrote.

So the body is *written*, once, by the reviewer — the one party that has just read
the whole diff and knows what it does. ``ppy review approve --pr-description <file>``
records it against the commit it approves, :func:`validate_description` refuses one
that is missing any of :data:`DESCRIPTION_SECTIONS` (or points a reader at files that
never leave the machine), and :func:`compose` uses it verbatim, adding only what the
runtime knows better than any author: where the change sits in its stack, and who
drove it. A delivery with no description for its head is refused before anything is
pushed (:class:`MissingDescription`); a person who wrote the whole body themselves
still passes ``ppy deliver --body-file``.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

#: Optional: the manager session's own link, appended to the attribution so a
#: reader can trace the pull request back to the conversation that drove it.
SESSION_URL_ENV = "PPY_SESSION_URL"
#: Optional: extra lines appended after the attribution (kept for callers that
#: already set it). The attribution itself never depends on it.
FOOTER_ENV = "PPY_PR_FOOTER"

#: The sections every pull request description carries, in this order. Each answers
#: a question the person reviewing or merging the change asks, in their words:
#: what is this, why does it exist, what changes for the people who use the product,
#: how do I see it working, and what should I not assume.
DESCRIPTION_SECTIONS = (
    "Summary",
    "Why",
    "Product impact",
    "How to test",
    "Risks and what was not verified",
)
#: The event a recorded description rides, on the worker's task, bound to a head.
DESCRIPTION_EVENT = "pr_description"
#: Below this a section is a label, not an answer ("n/a", "see diff").
MIN_SECTION_CHARS = 40
#: Paths that exist only on the machine that did the work. A reader of the pull
#: request cannot open them, so a description that cites them points at nothing.
_LOCAL_ONLY = (".ppy-evidence", "/private/tmp", ".treehouse/")

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s*(.+?)\s*$")
_BOLD_LINE = re.compile(r"^\s*\*\*(.+?)\*\*\s*:?\s*$")


def _normalize(text: str) -> str:
    return " ".join(text.replace("*", "").replace("_", "").split()).strip(" .:").lower()


def _heading_of(line: str) -> tuple[int | None, str | None]:
    """(depth, label) for a line that reads as a heading, else (None, None).

    A bold line on its own and a plain label line both count: workers write these
    sections all three ways, and a body that only understood ``##`` would drop two
    of them.
    """
    match = _HEADING.match(line)
    if match is not None:
        return len(match.group(1)), match.group(2).rstrip(" :")
    bold = _BOLD_LINE.match(line)
    if bold is not None:
        return 6, bold.group(1).rstrip(" :")
    return None, None


def _repo_name(conn: sqlite3.Connection, repo_id: int | None) -> str | None:
    if repo_id is None:
        return None
    row = conn.execute("SELECT name FROM repos WHERE id = ?", (repo_id,)).fetchone()
    return row["name"] if row is not None else None


def archived_brief(conn: sqlite3.Connection, task: sqlite3.Row) -> str:
    """The brief this task was dispatched with, as archived at dispatch time."""
    from papaya_agent_runtime.preflight import archived_brief_path

    repo = _repo_name(conn, task["repo_id"])
    if repo is None:
        return ""
    path: Path = archived_brief_path(repo, int(task["id"]))
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


class DescriptionError(ValueError):
    """A description that would not tell a reader what they need; says what is missing."""


class MissingDescription(DescriptionError):
    """No description was recorded for the commit being delivered."""


def sections_of(text: str) -> dict[str, str]:
    """Each top-level section's body, keyed by its normalised heading.

    The shallowest heading level in the text is the section level, so a description
    written with ``##`` and one written with ``#`` read the same, and a ``###`` inside
    a section stays part of it.
    """
    headed = [
        (index, depth, label)
        for index, line in enumerate(text.splitlines())
        for depth, label in [_heading_of(line)]
        if depth is not None and label is not None and _HEADING.match(line)
    ]
    if not headed:
        return {}
    top = min(depth for _, depth, _ in headed)
    lines = text.splitlines()
    marks = [(index, label) for index, depth, label in headed if depth == top]
    out: dict[str, str] = {}
    for position, (index, label) in enumerate(marks):
        stop = marks[position + 1][0] if position + 1 < len(marks) else len(lines)
        out[_normalize(label)] = "\n".join(lines[index + 1 : stop]).strip()
    return out


def validate_description(text: str) -> list[str]:
    """Every reason ``text`` would not serve a person reading the pull request.

    Empty means it will do. The checks are the ones a reader cannot recover from:
    a question left unanswered (a missing or token section), and a pointer to files
    that stayed on the machine that did the work.
    """
    problems: list[str] = []
    if not text.strip():
        return ["the description is empty"]
    found = sections_of(text)
    for title in DESCRIPTION_SECTIONS:
        body = found.get(_normalize(title))
        if body is None:
            problems.append(f'missing the "## {title}" section')
        elif len(body) < MIN_SECTION_CHARS:
            problems.append(
                f'"## {title}" says too little to help a reader ({len(body)} characters; '
                f"write at least {MIN_SECTION_CHARS})"
            )
    for marker in _LOCAL_ONLY:
        if marker in text:
            problems.append(
                f"it cites `{marker}`, which only exists on the machine that did the work; "
                "a reader of the pull request cannot open it — describe what it showed, or "
                "attach the file"
            )
    return problems


def record_description(task_id: int, head: str, text: str, *, conn: sqlite3.Connection) -> None:
    """Store ``text`` as the description for ``task_id`` at ``head``, or raise why not."""
    from papaya_agent_runtime.state import store

    problems = validate_description(text)
    if problems:
        raise DescriptionError("; ".join(problems))
    store.append_event(
        conn,
        kind=DESCRIPTION_EVENT,
        task_id=task_id,
        payload={"head_sha": head, "text": text.strip()},
    )


def description_for(conn: sqlite3.Connection, task_id: int, head: str) -> str:
    """The newest description recorded for exactly ``head``, or "".

    Bound to the commit like the approval it was written with: a description of an
    earlier head describes code that has since changed.
    """
    import json

    if not head:
        return ""
    rows = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND kind = ? ORDER BY id DESC",
        (task_id, DESCRIPTION_EVENT),
    ).fetchall()
    for row in rows:
        payload = json.loads(row["payload"] or "{}")
        recorded = str(payload.get("head_sha") or "")
        if recorded and (recorded.startswith(head) or head.startswith(recorded)):
            return str(payload.get("text") or "")
    return ""


def _default_branch(conn: sqlite3.Connection, repo_id: int | None) -> str:
    if repo_id is None:
        return "main"
    row = conn.execute("SELECT default_branch FROM repos WHERE id = ?", (repo_id,)).fetchone()
    if row is None:
        return "main"
    return (row["default_branch"] or "main").strip() or "main"


def _stack(conn: sqlite3.Connection, task: sqlite3.Row) -> str:
    stacked_on = ""
    if "stacked_on" in task.keys():  # noqa: SIM118 - sqlite3.Row: `in` scans values
        stacked_on = (task["stacked_on"] or "").strip()
    # A task dispatched with `--base main` records the default branch as its
    # starting point. That is the bottom of a stack, not a layer above one.
    if stacked_on == _default_branch(conn, task["repo_id"]):
        stacked_on = ""
    if stacked_on:
        lead = (
            f"This work was started from the branch `{stacked_on}` and opens against it, "
            "so it sits on top of the change on that branch rather than on the default "
            "branch. Merge that one first, then this."
        )
        # When the parent is a recorded task, name the whole chain in merge order:
        # ten reflections in one week said the brief should have carried this line.
        from papaya_agent_runtime import stacks

        order = stacks.merge_order(conn, task)
        if order:
            return f"{lead}\n\n{order[0].upper()}{order[1:]}"
        return lead
    return (
        "Bottom of its stack: this work started from the repository's default branch "
        "and targets it directly. Nothing has to land before it."
    )


def _worker_label(task: sqlite3.Row) -> str:
    keys = task.keys()
    provider = task["provider"] if "provider" in keys else None  # noqa: SIM118 - sqlite3.Row
    model = task["model"] if "model" in keys else None  # noqa: SIM118 - sqlite3.Row
    if provider and model:
        return f"a dispatched {provider} worker ({model})"
    if provider:
        return f"a dispatched {provider} worker"
    return "a dispatched worker"


def _driver(conn: sqlite3.Connection | None) -> str:
    """Who drove this, named the way the workspace addresses them.

    The runtime has no identity of its own — it is whichever Papaya agent this
    machine is connected as. When that connection exists, the credit belongs to
    that agent by handle, because a reviewer who wants to reply needs someone to
    reply *to*. Unconnected, it is the runtime itself.
    """
    from papaya_agent_runtime import papaya

    who = papaya.identity()
    if who is None:
        return "Papaya Agent Runtime"
    return f"{who.addressed} on Papaya Agent Runtime"


def _footer(task: sqlite3.Row, head: str, conn: sqlite3.Connection | None = None) -> str:
    """Who did this, in one sentence: the connected agent drove it, a worker built it.

    The credit belongs to the session that briefed the work, reviewed the exact
    commit, and delivered it — not to a generic harness line. Shane, 2026-09-04: the
    footer should name the runtime driving the work, and it now names the Papaya
    agent doing the driving whenever this machine is connected as one.
    """
    from papaya_agent_runtime import tracker

    attribution = (
        f"Driven by {_driver(conn)}: briefed, reviewed at commit `{head[:8]}`, and "
        f"delivered from the branch `{task['branch']}` by the runtime session; "
        f'implemented by {_worker_label(task)}. Tracked as "{task["title"]}".'
    )
    session = os.environ.get(SESSION_URL_ENV, "").strip()
    if session:
        attribution += f" Session: {session}"
    lines = [attribution]
    if conn is not None:
        tracked = tracker.link_sentence(tracker.task_link(conn, task["id"]))
        if tracked:
            lines.append(tracked)
    extra = os.environ.get(FOOTER_ENV, "").strip()
    if extra:
        lines.append(extra)
    return "\n\n".join(lines)


def compose(task_id: int, *, head_sha: str = "", conn: sqlite3.Connection | None = None) -> str:
    """The pull request body for a delivered task: its description, stack and credit.

    Raises :class:`MissingDescription` when no description was recorded for this
    head — delivery calls this before it pushes, so nothing leaves the machine with
    a body nobody wrote.
    """
    from papaya_agent_runtime.state import init_db, store

    conn = conn or init_db()
    task = store.get_task(conn, task_id)
    if task is None:
        raise ValueError(f"task {task_id} not found")
    head = head_sha or ""
    description = description_for(conn, task_id, head)
    if not description:
        sections = ", ".join(f'"{title}"' for title in DESCRIPTION_SECTIONS)
        raise MissingDescription(
            f"no pull request description was written for task {task_id} at "
            f"{head[:8] or 'its head'}. Approve it with `ppy review approve {task_id} "
            f"--pr-description <file>` — a description for people, with the sections "
            f"{sections} — or pass `ppy deliver {task_id} --body-file <file>` with a body "
            "you wrote yourself"
        )
    return (
        "\n\n".join(
            [
                description.strip(),
                "## Stack",
                _stack(conn, task),
                "---",
                _footer(task, head, conn),
            ]
        ).rstrip()
        + "\n"
    )


__all__ = [
    "DESCRIPTION_EVENT",
    "DESCRIPTION_SECTIONS",
    "FOOTER_ENV",
    "MIN_SECTION_CHARS",
    "SESSION_URL_ENV",
    "DescriptionError",
    "MissingDescription",
    "archived_brief",
    "compose",
    "description_for",
    "record_description",
    "sections_of",
    "validate_description",
]
