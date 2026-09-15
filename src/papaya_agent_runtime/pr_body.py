"""The pull request body a delivery writes, composed from what the run already holds.

Every delivery on 2026-09-04 opened its pull request with the body "Automated
delivery of task N (sha)." and the manager then rewrote it by hand from three things
the control plane was already holding: the brief the worker was dispatched with, the
worker's own closing and test reports, and the review that approved the commit. That
rewrite is where a reviewer's context came from, and it happened outside the tool, so
it was done differently every time and skipped whenever the day was busy.

This module composes that body instead:

- **Why** — the first section of the archived brief, after its opening heading.
- **What** — the worker's closing report — or, when the done note has not landed
  yet, its newest report with the phase named — plus anything it filed under
  "Outside scope, required to build" or "Flagged, not done" in an earlier report.
- **Verification** — the worker's last test report and the reviewer's approval note.
- **Stack** — the branch this work was dispatched from, when that branch is a layer
  below rather than the repository's own default branch, or a plain statement that
  it targets the default branch.

Everything is quoted from what people actually wrote; nothing is invented. Sections
with no source say so in a sentence rather than going missing, so a body is always
valid — and every reference is described in words, never left as a bare identifier a
reader outside the run would have to look up.
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

#: Length ceilings, so one enormous report cannot become an unreadable body.
WHY_CAP = 1500
WHAT_CAP = 4000
VERIFICATION_CAP = 2000

#: Headings a worker is asked to file, quoted verbatim when they appear.
CARRIED_HEADINGS = ("Outside scope, required to build", "Flagged, not done")

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


def _cap(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " … (trimmed)"


def first_section(text: str, *, own_title: str = "") -> str:
    """The brief's first section after its opening heading.

    A brief opens with the outcome and then explains why the work is worth doing;
    that explanation is exactly what a reviewer who has not seen the brief needs.
    Prose directly under the opening heading counts as that section; so does the
    first sub-section, whose own heading is kept as a bold lead line so its wording
    is not lost.

    ``own_title`` is the heading the caller is about to put this section under. A
    brief whose first sub-section is "## Why" would otherwise render under the
    composed "## Why" as a bold "**Why**" line saying the same word twice, so a lead
    that matches is dropped; any other label ("Problem", "Gap") is kept.
    """
    lines = text.splitlines()
    index = 0
    while index < len(lines) and _heading_of(lines[index])[0] is None:
        index += 1
    index += 1  # step past the opening heading (or past the end, harmlessly)
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines):
        return ""

    body: list[str] = []
    depth, label = _heading_of(lines[index])
    if label is not None:
        if not own_title or _normalize(label) != _normalize(own_title):
            body.append(f"**{label}**")
        index += 1
    for line in lines[index:]:
        line_depth, line_label = _heading_of(line)
        if line_label is not None and (depth is None or line_depth <= depth):
            break
        body.append(line)
    return "\n".join(body).strip()


def named_section(text: str, title: str) -> str:
    """The verbatim body under the heading ``title``, or "" when it is not there."""
    want = _normalize(title)
    lines = text.splitlines()
    for index, line in enumerate(lines):
        depth, label = _heading_of(line)
        if label is None and _normalize(line) == want and line.strip():
            depth, label = 6, line.strip()
        if label is None or _normalize(label) != want:
            continue
        body: list[str] = []
        for following in lines[index + 1 :]:
            next_depth, next_label = _heading_of(following)
            if next_label is not None and next_depth is not None and next_depth <= (depth or 6):
                break
            body.append(following)
        return "\n".join(body).strip()
    return ""


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


def _latest_note(notes: list[dict], phase: str) -> str:
    for entry in notes:  # newest first
        if entry.get("phase") == phase:
            return (entry.get("note") or "").strip()
    return ""


def _why(conn: sqlite3.Connection, task: sqlite3.Row) -> str:
    brief = archived_brief(conn, task)
    if brief.strip():
        section = first_section(brief, own_title="Why") or brief
        return _cap(section, WHY_CAP)
    return (
        "No brief was archived for this work, so there is nothing to quote here. "
        f'The objective it was dispatched with, in full: "{task["title"]}".'
    )


def _latest_report(notes: list[dict]) -> tuple[str, str]:
    """(note, phase) for the newest report that says anything, else ("", "")."""
    for entry in notes:  # newest first
        note = (entry.get("note") or "").strip()
        if note:
            return note, (entry.get("phase") or "").strip()
    return "", ""


def _what(notes: list[dict]) -> str:
    lead = _latest_note(notes, "done")
    if lead:
        parts = [lead]
    else:
        # A done note filed seconds after delivery is not in hand yet, but the
        # worker's last word is — quote it, and say which phase it came from so no
        # reader mistakes a mid-flight report for a closing one.
        lead, phase = _latest_report(notes)
        if not lead:
            return (
                "The worker filed no closing report, so there is nothing to quote here; "
                "the change itself is the diff on this branch."
            )
        phase_label = f"`{phase}`" if phase else "its last recorded"
        parts = [f"The worker's latest report, filed at the {phase_label} phase:\n\n{lead}"]
    for heading in CARRIED_HEADINGS:
        if _normalize(heading) in _normalize(lead):
            continue  # the report already quoted above carries it, verbatim
        for entry in notes:
            carried = named_section(entry.get("note") or "", heading)
            if carried:
                parts.append(f"**{heading}**\n\n{carried}")
                break
    return _cap("\n\n".join(parts), WHAT_CAP)


def _verification(notes: list[dict], approval: str, head: str) -> str:
    parts: list[str] = []
    test_note = _latest_note(notes, "test")
    if test_note:
        parts.append(f"The worker's last verification report:\n\n{test_note}")
    if approval:
        parts.append(f"The reviewer approved this exact commit and wrote:\n\n{approval}")
    if not parts:
        parts.append(
            "Neither a verification report nor a note from the reviewer was filed. "
            "Delivery still required an approval bound to the exact commit below, "
            "which is the only way work leaves the machine."
        )
    else:
        parts.append(
            f"Delivery was gated on an approval bound to commit {head[:8]}, the commit "
            "this pull request carries."
        )
    return _cap("\n\n".join(parts), VERIFICATION_CAP)


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
    """The full pull request body for a delivered task."""
    from papaya_agent_runtime import progress
    from papaya_agent_runtime.review import approval_note
    from papaya_agent_runtime.state import init_db, store

    conn = conn or init_db()
    task = store.get_task(conn, task_id)
    if task is None:
        raise ValueError(f"task {task_id} not found")
    notes = progress.history(task_id, conn=conn)
    head = head_sha or ""

    return (
        "\n\n".join(
            [
                "## Why",
                _why(conn, task),
                "## What",
                _what(notes),
                "## Verification",
                _verification(notes, approval_note(task_id), head),
                "## Stack",
                _stack(conn, task),
                "---",
                _footer(task, head, conn),
            ]
        ).rstrip()
        + "\n"
    )


__all__ = [
    "CARRIED_HEADINGS",
    "FOOTER_ENV",
    "SESSION_URL_ENV",
    "archived_brief",
    "compose",
    "first_section",
    "named_section",
]
