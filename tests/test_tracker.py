"""A task's tracked record, in whatever tracker the workspace actually uses.

Papaya work items are the default, never the assumption. A workspace that tracks
work in Linear is telling every agent something true, and a runtime that hard-codes
one tracker overrides it quietly — first in the pull request bodies it writes, then
in the habits it teaches. These pin that the runtime stays a recorder: it names
where the work lives and never decides it.
"""

from __future__ import annotations

import pytest

from papaya_agent_runtime import tracker
from papaya_agent_runtime.state import init_db, store


def _task(conn, title: str = "Add a health endpoint") -> int:
    repo_id = store.add_repo(
        conn, name="app", origin="/o", local_path="/l", default_branch="main", base_sha="a"
    )
    run_id = store.create_run(conn, "ship it")
    return store.add_task(conn, run_id=run_id, title=title, repo_id=repo_id, ends_at="done")


def test_a_linear_record_round_trips_with_its_provider(ppy_home) -> None:
    conn = init_db()
    task_id = _task(conn)
    tracker.link_task(
        conn,
        task_id,
        record="ENG-1183",
        provider="linear",
        url="https://linear.app/acme/issue/ENG-1183",
        title="Health endpoint for the API",
    )
    assert tracker.task_link(conn, task_id) == {
        "record": "ENG-1183",
        "provider": "linear",
        "url": "https://linear.app/acme/issue/ENG-1183",
        "title": "Health endpoint for the API",
    }


def test_papaya_is_the_default_only_when_nobody_has_said(ppy_home) -> None:
    conn = init_db()
    task_id = _task(conn)
    tracker.link_task(conn, task_id, record="PAP-214")
    link = tracker.task_link(conn, task_id)
    assert link is not None and link["provider"] == "papaya"


def test_an_unlinked_task_has_no_link(ppy_home) -> None:
    conn = init_db()
    assert tracker.task_link(conn, _task(conn, title="Small fix")) is None
    assert tracker.link_sentence(None) == ""


def test_the_sentence_names_the_tracker_so_a_reviewer_knows_where_to_look() -> None:
    sentence = tracker.link_sentence(
        {
            "record": "ENG-1183",
            "provider": "linear",
            "url": "https://linear.app/acme/issue/ENG-1183",
            "title": "QA sweep",
        }
    )
    assert "Linear" in sentence
    assert '"QA sweep"' in sentence  # described, never a bare identifier
    assert "https://linear.app/acme/issue/ENG-1183" in sentence


def test_a_tracker_nobody_wrote_down_still_renders_as_itself() -> None:
    """A closed provider list would refuse workspaces we have never heard of."""
    sentence = tracker.link_sentence({"record": "W-9", "provider": "clubhouse", "title": "Thing"})
    assert "clubhouse" in sentence


@pytest.mark.parametrize(
    ("provider", "expected"),
    [("linear", "Linear"), ("LINEAR", "Linear"), ("papaya", "Papaya"), ("", "Papaya")],
)
def test_provider_labels_are_case_insensitive_and_default(provider: str, expected: str) -> None:
    assert tracker.label(provider) == expected


def test_a_record_with_no_title_falls_back_to_its_id_rather_than_nothing() -> None:
    sentence = tracker.link_sentence({"record": "ENG-1183", "provider": "linear"})
    assert "ENG-1183" in sentence


def test_the_pull_request_footer_names_whatever_tracker_was_recorded(ppy_home) -> None:
    """The footer is where a tracker assumption would leak to people outside the workspace."""
    from papaya_agent_runtime import pr_body

    conn = init_db()
    task_id = _task(conn)
    store.update_task_fields(conn, task_id, branch="ppy/task-1-abc")
    tracker.link_task(conn, task_id, record="ENG-1183", provider="linear", title="Health endpoint")
    body = pr_body.compose(task_id, head_sha="c" * 40, conn=conn)
    assert "Tracked in Linear" in body
    assert "Papaya work item" not in body
