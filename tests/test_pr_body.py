"""The pull request body is a description the reviewer wrote for people, not quotes.

PAP-279's PR #811 (2026-09-21) went out with a body quoted from the worker's last
progress note and the reviewer's shorthand: nothing about what changed for a user or
how to check it. These tests hold the replacement: the reviewer writes five sections,
approval refuses one that falls short, and delivery refuses a head without one before
it pushes anything.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from papaya_agent_runtime import cli, delivery, pr_body, review
from papaya_agent_runtime.state import init_db, store

DESCRIPTION = """## Summary

Activity cards now say when someone commented on your work item, naming who, on web
and iOS.

## Why

The backend began sending a "commented" reason in PAP-276, and both clients showed it
as a generic card. Requested on PAP-279.

## Product impact

People see "@dana commented on PAP-12" in Activity; tapping it opens the item scrolled
to that comment. Needs a new iOS build to reach phones.

## How to test

1. As one person, comment on a work item assigned to someone else.
2. As the assignee, open Activity: the card names the commenter.
3. Tap it: the item opens at that comment, highlighted.

## Risks and what was not verified

iOS was checked by unit tests of the mechanism only; nobody tapped it on a device.
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


@pytest.fixture
def described(task):
    """A task whose reviewer recorded a description at every head these tests use."""
    for head in ("b" * 40, "c" * 40, "f" * 40):
        pr_body.record_description(task.id, head, DESCRIPTION, conn=task.conn)
    return task


# --------------------------------------------------------------------------- #
# What a description has to say
# --------------------------------------------------------------------------- #


def test_a_description_with_all_five_sections_is_accepted() -> None:
    assert pr_body.validate_description(DESCRIPTION) == []


def test_every_missing_section_is_named() -> None:
    problems = pr_body.validate_description("## Summary\n\n" + "Changed a thing. " * 5)
    for title in ("Why", "Product impact", "How to test", "Risks and what was not verified"):
        assert f'missing the "## {title}" section' in problems


def test_a_section_that_is_only_a_label_is_refused() -> None:
    thin = DESCRIPTION.replace(
        "1. As one person, comment on a work item assigned to someone else.\n"
        "2. As the assignee, open Activity: the card names the commenter.\n"
        "3. Tap it: the item opens at that comment, highlighted.",
        "See diff.",
    )
    (problem,) = pr_body.validate_description(thin)
    assert '"## How to test" says too little' in problem


def test_a_path_only_this_machine_has_is_refused() -> None:
    """#811 pointed its reader at .ppy-evidence/*.png, which never left the machine."""
    local = DESCRIPTION + "\nScreenshot: .ppy-evidence/web-commented-card.png\n"
    (problem,) = pr_body.validate_description(local)
    assert "`.ppy-evidence`" in problem and "cannot open it" in problem


def test_sections_are_read_at_the_shallowest_heading_level() -> None:
    """A `###` inside a section is part of it, and `#`-level sections read the same."""
    nested = DESCRIPTION.replace("## How to test\n", "## How to test\n\n### Web\n")
    assert pr_body.validate_description(nested) == []
    assert pr_body.validate_description(DESCRIPTION.replace("## ", "# ")) == []


# --------------------------------------------------------------------------- #
# The body is the description, bound to the head it was written for
# --------------------------------------------------------------------------- #


def test_the_body_is_the_description_then_the_stack_then_the_credit(described) -> None:
    body = pr_body.compose(described.id, head_sha="b" * 40, conn=described.conn)
    assert body.startswith("## Summary\n\nActivity cards now say")
    assert body.index("## Risks and what was not verified") < body.index("## Stack")
    assert body.index("## Stack") < body.index("Driven by")
    assert body.endswith("\n")


def test_no_description_at_this_head_is_refused_with_the_command_to_fix_it(task) -> None:
    pr_body.record_description(task.id, "b" * 40, DESCRIPTION, conn=task.conn)
    with pytest.raises(pr_body.MissingDescription) as refused:
        pr_body.compose(task.id, head_sha="9" * 40, conn=task.conn)
    message = str(refused.value)
    assert f"ppy review approve {task.id}" in message and "--pr-description" in message
    assert "--body-file" in message


def test_the_newest_description_for_a_head_wins(task) -> None:
    pr_body.record_description(task.id, "b" * 40, DESCRIPTION, conn=task.conn)
    rewritten = DESCRIPTION.replace("Activity cards now say", "Activity cards finally say")
    pr_body.record_description(task.id, "b" * 40, rewritten, conn=task.conn)
    body = pr_body.compose(task.id, head_sha="b" * 40, conn=task.conn)
    assert "finally say" in body


def test_recording_a_description_that_falls_short_is_refused(task) -> None:
    with pytest.raises(pr_body.DescriptionError, match="missing the"):
        pr_body.record_description(task.id, "b" * 40, "## Summary\n\nStuff.", conn=task.conn)
    assert pr_body.description_for(task.conn, task.id, "b" * 40) == ""


# --------------------------------------------------------------------------- #
# Approval takes the description, and checks it before approving
# --------------------------------------------------------------------------- #


def _approve(monkeypatch, task_id: int, description_path) -> int:
    from papaya_agent_runtime import supervision

    monkeypatch.setattr(supervision, "full_suite_missing", lambda tid: "")
    monkeypatch.setattr(review, "head_sha", lambda wt: "b" * 40)
    return cli.main(["review", "approve", str(task_id), "--pr-description", str(description_path)])


def test_approve_records_the_description_against_the_approved_head(
    task, tmp_path, monkeypatch
) -> None:
    path = tmp_path / "pr.md"
    path.write_text(DESCRIPTION)
    assert _approve(monkeypatch, task.id, path) == 0
    assert review.approval_note(task.id) == ""  # approved, with no --note
    assert "Activity cards now say" in pr_body.description_for(init_db(), task.id, "b" * 40)


def test_approve_refuses_a_thin_description_and_records_no_approval(
    task, tmp_path, monkeypatch, capsys
) -> None:
    path = tmp_path / "pr.md"
    path.write_text("## Summary\n\nFixed it.\n")
    assert _approve(monkeypatch, task.id, path) == 1
    assert "would not help the person reading it" in capsys.readouterr().err
    assert review.latest_review(task.id) is None


def test_approve_without_a_description_is_a_usage_error(task) -> None:
    with pytest.raises(SystemExit):
        cli.main(["review", "approve", str(task.id)])


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #


def _fake_gh(monkeypatch, calls: list[list[str]]) -> None:
    def fake_run(argv, cwd=None):
        calls.append(argv)
        if argv[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")  # no open PR yet
        return SimpleNamespace(returncode=0, stdout="https://example/pr/1\n", stderr="")

    monkeypatch.setattr(delivery, "_run", fake_run)
    monkeypatch.setattr(delivery, "is_approved_at_head", lambda tid: (True, ""))
    monkeypatch.setattr(delivery, "head_sha", lambda wt: "f" * 40)
    monkeypatch.setattr(delivery, "_pr_tool", lambda: "gh")


def test_a_body_file_and_a_title_override_everything(task, tmp_path, monkeypatch) -> None:
    calls: list[list[str]] = []
    _fake_gh(monkeypatch, calls)
    handwritten = tmp_path / "body.md"
    handwritten.write_text("Written by hand, and not to be improved upon.\n")

    delivery.deliver(task.id, title="A title I chose", body_file=str(handwritten))

    argv = next(a for a in calls if a[:3] == ["gh", "pr", "create"])
    assert argv[argv.index("--body") + 1] == "Written by hand, and not to be improved upon.\n"
    assert argv[argv.index("--title") + 1] == "A title I chose"


def test_deliver_opens_the_pull_request_with_the_description(described, monkeypatch) -> None:
    calls: list[list[str]] = []
    _fake_gh(monkeypatch, calls)

    delivery.deliver(described.id)

    argv = next(a for a in calls if a[:3] == ["gh", "pr", "create"])
    body = argv[argv.index("--body") + 1]
    assert body.startswith("## Summary")
    assert "## How to test" in body
    assert argv[argv.index("--title") + 1] == "Rebuild the review surface"


def test_deliver_refuses_before_pushing_when_nothing_was_written(task, monkeypatch) -> None:
    calls: list[list[str]] = []
    _fake_gh(monkeypatch, calls)

    with pytest.raises(delivery.DeliveryError, match="no pull request description"):
        delivery.deliver(task.id)

    assert not any(a[:2] == ["git", "push"] for a in calls)
    assert not any(a[:3] == ["gh", "pr", "create"] for a in calls)


# --------------------------------------------------------------------------- #
# What the runtime adds: the stack and the credit
# --------------------------------------------------------------------------- #


def test_the_stack_section_names_the_branch_the_work_was_started_from(described) -> None:
    store.update_task_fields(described.conn, described.id, stacked_on="ppy/task-7-abc")
    body = pr_body.compose(described.id, head_sha="b" * 40, conn=described.conn)
    assert "`ppy/task-7-abc`" in body
    assert "Merge that one first" in body
    assert "Bottom of its stack" not in body


def test_a_task_started_from_the_default_branch_is_the_bottom_of_its_stack(described) -> None:
    """`--base main` records the default branch, which is not a layer below."""
    store.update_task_fields(described.conn, described.id, stacked_on="main")
    body = pr_body.compose(described.id, head_sha="b" * 40, conn=described.conn)
    assert "Bottom of its stack" in body
    assert "Merge that one first" not in body


def test_the_stack_section_states_the_merge_order_when_the_parent_is_a_task(described) -> None:
    """A recorded stack parent is named in order, not just as a branch (issue #61)."""
    parent = store.add_task(described.conn, run_id=1, title="bottom layer", repo_id=1)
    store.update_task_fields(described.conn, parent, branch="ppy/task-7-abc")
    store.update_task_fields(
        described.conn, described.id, stacked_on="ppy/task-7-abc", stacked_on_task=parent
    )
    body = pr_body.compose(described.id, head_sha="b" * 40, conn=described.conn)
    assert "Merge that one first" in body
    assert "Stack: layer 2 of 2; its pull request targets ppy/task-7-abc." in body
    assert f'task {parent} "bottom layer" (branch ppy/task-7-abc, unmerged)' in body
    assert f"then this task {described.id}." in body

    store.update_task_fields(described.conn, parent, merged_at="2026-09-06T00:00:00+00:00")
    body = pr_body.compose(described.id, head_sha="b" * 40, conn=described.conn)
    assert "(branch ppy/task-7-abc, merged)" in body


def test_the_footer_credits_the_runtime_and_the_worker_by_default(described, monkeypatch) -> None:
    monkeypatch.delenv(pr_body.SESSION_URL_ENV, raising=False)
    body = pr_body.compose(described.id, head_sha="c" * 40, conn=described.conn)
    assert "Driven by Papaya Agent Runtime" in body
    assert "implemented by a dispatched" in body
    assert "Generated with" not in body


def test_the_session_link_rides_the_attribution_when_supplied(described, monkeypatch) -> None:
    monkeypatch.setenv(pr_body.SESSION_URL_ENV, "https://example/session")
    body = pr_body.compose(described.id, head_sha="c" * 40, conn=described.conn)
    assert "Driven by Papaya Agent Runtime" in body
    assert "Session: https://example/session" in body


def test_extra_footer_lines_are_appended_after_the_attribution(described, monkeypatch) -> None:
    monkeypatch.setenv(pr_body.FOOTER_ENV, "Deployed by the release train")
    body = pr_body.compose(described.id, head_sha="c" * 40, conn=described.conn)
    assert body.index("Driven by Papaya Agent Runtime") < body.index(
        "Deployed by the release train"
    )
