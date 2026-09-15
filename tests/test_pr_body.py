"""`ppy deliver` writes the pull request body from the brief, the reports, and the review."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from papaya_agent_runtime import delivery, pr_body, preflight, progress, review
from papaya_agent_runtime.state import init_db, store

BRIEF = """# Rebuild the review surface

## Gap (every delivery this week)

The pull request body said only that the delivery was automated, so a reviewer who
had not seen the brief had nothing to read.

## Proposal

Compose the body from what the run already holds.
"""

DONE_NOTE = """Rebuilt the surface. The panel now renders the worker's own report first.

## Outside scope, required to build

Bumped the linter pin; the old one could not parse the new syntax.

## Flagged, not done

The dark theme still has a contrast problem on the diffstat.
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PPY_HOME", str(tmp_path / ".ppy"))
    monkeypatch.delenv(pr_body.FOOTER_ENV, raising=False)
    monkeypatch.delenv(pr_body.SESSION_URL_ENV, raising=False)
    return init_db()


@pytest.fixture
def task(home, tmp_path):
    conn = home
    repo_id = store.add_repo(
        conn,
        name="papaya",
        origin="git@example.com:acme/papaya.git",
        local_path=str(tmp_path / "clone"),
        default_branch="main",
        base_sha="a" * 40,
    )
    run_id = store.create_run(conn, "ship it")
    task_id = store.add_task(
        conn, run_id=run_id, title="Rebuild the review surface", repo_id=repo_id
    )
    store.update_task_fields(
        conn,
        task_id,
        worktree_path=str(tmp_path / "wt"),
        branch="ppy/task-1-abc",
        base_sha="a" * 40,
    )
    return SimpleNamespace(id=task_id, conn=conn)


# --------------------------------------------------------------------------- #
# The parts
# --------------------------------------------------------------------------- #


def test_first_section_takes_the_sub_section_after_the_opening_heading() -> None:
    section = pr_body.first_section(BRIEF)
    assert section.startswith("**Gap (every delivery this week)**")
    assert "nothing to read" in section
    assert "Proposal" not in section  # the next section of the same depth ends it


def test_first_section_takes_prose_written_straight_under_the_heading() -> None:
    section = pr_body.first_section("# Title\n\nThe reason, in prose.\n\n## Next\n\nMore.\n")
    assert section == "The reason, in prose."


def test_first_section_is_empty_when_the_heading_is_all_there_is() -> None:
    assert pr_body.first_section("# Title only\n") == ""


def test_named_section_quotes_the_body_verbatim_however_the_heading_is_written() -> None:
    assert "Bumped the linter pin" in pr_body.named_section(
        DONE_NOTE, "Outside scope, required to build"
    )
    bold = "Done.\n\n**Flagged, not done**\n\nThe contrast problem stands.\n"
    assert pr_body.named_section(bold, "Flagged, not done") == "The contrast problem stands."
    plain = "Done.\n\nFlagged, not done:\n\nOne thing left.\n"
    assert pr_body.named_section(plain, "Flagged, not done") == "One thing left."
    assert pr_body.named_section("nothing here", "Flagged, not done") == ""


# --------------------------------------------------------------------------- #
# The composed body
# --------------------------------------------------------------------------- #


def test_the_body_is_composed_from_the_brief_the_reports_and_the_approval(
    task, monkeypatch
) -> None:
    preflight.archive_brief("papaya", task.id, BRIEF)
    progress.record(task.id, phase="test", note="Full suite: 326 passed.", conn=task.conn)
    progress.record(task.id, phase="done", note=DONE_NOTE, conn=task.conn)
    monkeypatch.setattr(review, "head_sha", lambda _wt: "b" * 40)
    review.record_review(task.id, "approved", note="Opened both captures; spacing is right.")

    body = pr_body.compose(task.id, head_sha="b" * 40, conn=task.conn)

    assert "## Why" in body
    assert "nothing to read" in body  # from the brief's first section
    assert "The panel now renders the worker's own report first." in body
    assert "Bumped the linter pin" in body
    assert "contrast problem" in body
    assert "Full suite: 326 passed." in body
    assert "Opened both captures; spacing is right." in body
    assert "Bottom of its stack" in body
    assert "ppy/task-1-abc" in body
    # Plain references: no bare task identifier stands in for the work.
    assert f"task {task.id}" not in body
    assert "Automated delivery" not in body


def test_a_section_the_closing_report_missed_is_carried_from_an_earlier_report(
    task, monkeypatch
) -> None:
    progress.record(
        task.id,
        phase="implement",
        note="Halfway.\n\n## Flagged, not done\n\nThe migration needs a second pass.\n",
        conn=task.conn,
    )
    progress.record(task.id, phase="done", note="Finished the panel.", conn=task.conn)
    body = pr_body.compose(task.id, head_sha="b" * 40, conn=task.conn)
    assert "Finished the panel." in body
    assert "**Flagged, not done**" in body
    assert "The migration needs a second pass." in body


def test_a_brief_whose_first_section_is_why_says_why_once(task) -> None:
    """The composed "## Why" already names the section; a bold lead repeats the word."""
    preflight.archive_brief(
        "papaya",
        task.id,
        "# Rebuild the review surface\n\n## Why\n\nShane, 2026-09-04: every body was "
        "rewritten by hand.\n\n## In scope\n\nThe three fixes.\n",
    )
    body = pr_body.compose(task.id, head_sha="b" * 40, conn=task.conn)
    assert "**Why**" not in body
    assert body.count("## Why") == 1
    assert "Shane, 2026-09-04: every body was rewritten by hand." in body
    assert "The three fixes." not in body  # the next section of the same depth ends it


def test_a_brief_whose_first_section_has_another_name_keeps_its_bold_lead(task) -> None:
    preflight.archive_brief(
        "papaya",
        task.id,
        "# Rebuild the review surface\n\n## Problem\n\nThe body said only that the "
        "delivery was automated.\n",
    )
    body = pr_body.compose(task.id, head_sha="b" * 40, conn=task.conn)
    assert "**Problem**" in body
    assert "The body said only that the delivery was automated." in body


def test_the_stack_section_names_the_branch_the_work_was_started_from(task) -> None:
    store.update_task_fields(task.conn, task.id, stacked_on="ppy/task-7-abc")
    body = pr_body.compose(task.id, head_sha="b" * 40, conn=task.conn)
    assert "`ppy/task-7-abc`" in body
    assert "Merge that one first" in body
    assert "Bottom of its stack" not in body


def test_a_task_started_from_the_default_branch_is_the_bottom_of_its_stack(task) -> None:
    """`--base main` records the default branch, which is not a layer below."""
    store.update_task_fields(task.conn, task.id, stacked_on="main")
    body = pr_body.compose(task.id, head_sha="b" * 40, conn=task.conn)
    assert "Bottom of its stack" in body
    assert "Merge that one first" not in body


def test_the_newest_report_stands_in_when_the_done_note_has_not_landed(task) -> None:
    """Task 114: the closing report arrived seconds after delivery, not before it."""
    progress.record(task.id, phase="implement", note="Halfway.", conn=task.conn)
    progress.record(task.id, phase="review", note="Self-review clean; suite green.", conn=task.conn)
    body = pr_body.compose(task.id, head_sha="b" * 40, conn=task.conn)
    assert "The worker's latest report, filed at the `review` phase:" in body
    assert "Self-review clean; suite green." in body
    assert "filed no closing report" not in body


def test_a_task_with_no_brief_and_no_reports_still_produces_a_valid_body(task) -> None:
    body = pr_body.compose(task.id, head_sha="c" * 40, conn=task.conn)
    for heading in ("## Why", "## What", "## Verification", "## Stack"):
        assert heading in body
    assert "No brief was archived" in body
    assert '"Rebuild the review surface"' in body
    assert "filed no closing report" in body
    assert "Neither a verification report nor a note" in body
    assert body.endswith("\n")


def test_the_footer_credits_the_runtime_and_the_worker_by_default(task, monkeypatch) -> None:
    monkeypatch.delenv(pr_body.SESSION_URL_ENV, raising=False)
    body = pr_body.compose(task.id, head_sha="c" * 40, conn=task.conn)
    assert "Driven by Papaya Agent Runtime" in body
    assert "implemented by a dispatched" in body
    assert "Generated with" not in body


def test_the_session_link_rides_the_attribution_when_supplied(task, monkeypatch) -> None:
    monkeypatch.setenv(pr_body.SESSION_URL_ENV, "https://example/session")
    body = pr_body.compose(task.id, head_sha="c" * 40, conn=task.conn)
    assert "Driven by Papaya Agent Runtime" in body
    assert "Session: https://example/session" in body


def test_extra_footer_lines_are_appended_after_the_attribution(task, monkeypatch) -> None:
    monkeypatch.setenv(pr_body.FOOTER_ENV, "Deployed by the release train")
    body = pr_body.compose(task.id, head_sha="c" * 40, conn=task.conn)
    assert body.index("Driven by Papaya Agent Runtime") < body.index(
        "Deployed by the release train"
    )


# --------------------------------------------------------------------------- #
# Delivery uses it
# --------------------------------------------------------------------------- #


def _fake_gh(monkeypatch, calls: list[list[str]]) -> None:
    def fake_run(argv, cwd=None):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="https://example/pr/1\n", stderr="")

    monkeypatch.setattr(delivery, "_run", fake_run)
    monkeypatch.setattr(delivery, "is_approved_at_head", lambda tid: (True, ""))
    monkeypatch.setattr(delivery, "head_sha", lambda wt: "f" * 40)
    monkeypatch.setattr(delivery, "_pr_tool", lambda: "gh")


def test_deliver_opens_the_pull_request_with_the_composed_body(task, monkeypatch) -> None:
    preflight.archive_brief("papaya", task.id, BRIEF)
    progress.record(task.id, phase="done", note=DONE_NOTE, conn=task.conn)
    calls: list[list[str]] = []
    _fake_gh(monkeypatch, calls)

    delivery.deliver(task.id)

    argv = next(a for a in calls if a[:3] == ["gh", "pr", "create"])
    body = argv[argv.index("--body") + 1]
    assert "## Why" in body
    assert "nothing to read" in body
    assert argv[argv.index("--title") + 1] == "Rebuild the review surface"


def test_a_body_file_and_a_title_override_everything(task, tmp_path, monkeypatch) -> None:
    preflight.archive_brief("papaya", task.id, BRIEF)
    calls: list[list[str]] = []
    _fake_gh(monkeypatch, calls)
    handwritten = tmp_path / "body.md"
    handwritten.write_text("Written by hand, and not to be improved upon.\n")

    delivery.deliver(task.id, title="A title I chose", body_file=str(handwritten))

    argv = next(a for a in calls if a[:3] == ["gh", "pr", "create"])
    assert argv[argv.index("--body") + 1] == "Written by hand, and not to be improved upon.\n"
    assert argv[argv.index("--title") + 1] == "A title I chose"


def test_the_stack_section_states_the_merge_order_when_the_parent_is_a_task(task) -> None:
    """A recorded stack parent is named in order, not just as a branch (issue #61)."""
    parent = store.add_task(task.conn, run_id=1, title="bottom layer", repo_id=1)
    store.update_task_fields(task.conn, parent, branch="ppy/task-7-abc")
    store.update_task_fields(
        task.conn, task.id, stacked_on="ppy/task-7-abc", stacked_on_task=parent
    )
    body = pr_body.compose(task.id, head_sha="b" * 40, conn=task.conn)
    assert "Merge that one first" in body
    assert "Stack: layer 2 of 2; its pull request targets ppy/task-7-abc." in body
    assert f'task {parent} "bottom layer" (branch ppy/task-7-abc, unmerged)' in body
    assert f"then this task {task.id}." in body

    store.update_task_fields(task.conn, parent, merged_at="2026-09-06T00:00:00+00:00")
    body = pr_body.compose(task.id, head_sha="b" * 40, conn=task.conn)
    assert "(branch ppy/task-7-abc, merged)" in body
