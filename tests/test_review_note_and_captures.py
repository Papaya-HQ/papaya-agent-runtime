"""An approval carries the reviewer's words, and `ppy review show` lists the receipts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from conftest import PR_DESCRIPTION
from papaya_agent_runtime import captures, cli, progress, review
from papaya_agent_runtime.state import init_db, store
from papaya_agent_runtime.state.db import _column_names


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PPY_HOME", str(tmp_path / ".ppy"))
    return init_db()


@pytest.fixture
def task(home, tmp_path):
    conn = home
    run_id = store.create_run(conn, "ship it")
    task_id = store.add_task(conn, run_id=run_id, title="build the panel")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    store.update_task_fields(
        conn, task_id, worktree_path=str(worktree), branch="ppy/task-1-abc", base_sha="a" * 40
    )
    return SimpleNamespace(id=task_id, conn=conn, worktree=worktree)


def _stub_bundle(monkeypatch) -> None:
    monkeypatch.setattr(
        review,
        "build_bundle",
        lambda tid: SimpleNamespace(
            task_id=tid, base_sha="a" * 40, head_sha="b" * 40, files_changed=1, diffstat="1 file"
        ),
    )


# --------------------------------------------------------------------------- #
# The extractor
# --------------------------------------------------------------------------- #


def test_absolute_paths_are_evidence_whatever_they_are_called() -> None:
    note = "Wrote the run log to /tmp/run-104/output.txt and the shots to /tmp/run-104/shots."
    assert captures.capture_paths([note]) == ["/tmp/run-104/output.txt", "/tmp/run-104/shots"]


def test_the_contract_roots_are_evidence_when_written_relative() -> None:
    note = "Captures live in `docs/evidence/task-104/`; the table is docs/generated/summary.md."
    assert captures.capture_paths([note]) == [
        "docs/evidence/task-104",
        "docs/generated/summary.md",
    ]


def test_a_directory_the_note_calls_receipts_counts_however_it_is_spelled() -> None:
    note = (
        "See [the before shot](artifacts/captures/before.png) and artifacts/captures/after.png. "
        "Command output is under tmp/receipts/, screenshots/panel.png is the new panel."
    )
    assert captures.capture_paths([note]) == [
        "artifacts/captures/before.png",
        "artifacts/captures/after.png",
        "tmp/receipts",
        "screenshots/panel.png",
    ]


def test_prose_urls_and_ordinary_source_paths_are_not_evidence() -> None:
    note = (
        "Rewrote src/papaya_agent_runtime/cli.py and tests/test_cli.py; see "
        "https://example.com/docs/evidence/nope.png for background. No captures yet."
    )
    assert captures.capture_paths([note]) == []


def test_a_path_named_twice_across_reports_is_listed_once() -> None:
    notes = ["will write docs/evidence/x.png", "wrote docs/evidence/x.png, plus docs/evidence/y"]
    assert captures.capture_paths(notes) == ["docs/evidence/x.png", "docs/evidence/y"]


def test_render_lists_sizes_marks_the_missing_and_hands_back_image_paths(tmp_path) -> None:
    shots = tmp_path / "docs" / "evidence"
    shots.mkdir(parents=True)
    (shots / "after.png").write_bytes(b"x" * 2048)
    (shots / "log.txt").write_text("output\n")
    out = captures.render(["docs/evidence", "docs/evidence/gone.png"], root=tmp_path)
    assert "docs/evidence — directory, 2 item(s)" in out
    assert "after.png  2.0 KB" in out
    assert "docs/evidence/gone.png — (not found)" in out
    assert str(shots / "after.png") in out.splitlines()  # a bare, openable line
    assert str(shots / "log.txt") not in out.splitlines()


def test_render_says_nothing_when_no_receipts_were_named() -> None:
    assert captures.render([]) == ""


# --------------------------------------------------------------------------- #
# The approval note
# --------------------------------------------------------------------------- #


def test_reviews_gain_a_note_column_on_fresh_and_existing_databases(home) -> None:
    conn = home
    assert "note" in _column_names(conn, "reviews")
    conn.execute("ALTER TABLE reviews DROP COLUMN note")
    conn.commit()
    assert "note" not in _column_names(conn, "reviews")
    conn = init_db()
    assert "note" in _column_names(conn, "reviews")
    init_db()  # idempotent on a database that already has it


def test_the_approval_note_round_trips_and_is_bound_to_the_reviewed_commit(
    task, monkeypatch, capsys, tmp_path
) -> None:
    monkeypatch.setattr(review, "head_sha", lambda _wt: "c" * 40)
    description = tmp_path / "pr.md"
    description.write_text(PR_DESCRIPTION)
    rc = cli.main(
        [
            "review",
            "approve",
            str(task.id),
            "--note",
            "opened both captures; spacing is right",
            "--pr-description",
            str(description),
        ]
    )
    assert rc == 0
    assert "opened both captures" in capsys.readouterr().out

    stored = review.latest_review(task.id)
    assert stored["note"] == "opened both captures; spacing is right"
    assert stored["head_sha"] == "c" * 40
    assert review.approval_note(task.id) == "opened both captures; spacing is right"

    assert cli.main(["review", "status", str(task.id)]) == 0
    assert "opened both captures" in capsys.readouterr().out


def test_a_changes_requested_verdict_hides_the_earlier_approval_note(task, monkeypatch) -> None:
    monkeypatch.setattr(review, "head_sha", lambda _wt: "c" * 40)
    review.record_review(task.id, "approved", note="looked fine")
    assert review.approval_note(task.id) == "looked fine"
    review.record_review(task.id, "changes_requested", "the panel still overflows")
    assert review.approval_note(task.id) == ""


def test_review_show_lists_the_receipts_and_the_standing_approval_note(
    task, monkeypatch, capsys
) -> None:
    _stub_bundle(monkeypatch)
    monkeypatch.setattr(review, "head_sha", lambda _wt: "b" * 40)
    shots = task.worktree / "docs" / "evidence"
    shots.mkdir(parents=True)
    (shots / "panel.png").write_bytes(b"y" * 512)
    progress.record(task.id, phase="test", note="suite green", conn=task.conn)
    progress.record(
        task.id,
        phase="done",
        note="Panel rebuilt. Captures in docs/evidence/, plus docs/evidence/missing.png.",
        conn=task.conn,
    )
    review.record_review(task.id, "approved", note="opened the panel capture")

    assert cli.main(["review", "show", str(task.id)]) == 0
    out = capsys.readouterr().out
    assert "captures and receipts named in the worker's reports:" in out
    assert "panel.png  512 B" in out
    assert "docs/evidence/missing.png — (not found)" in out
    assert str(shots / "panel.png") in out.splitlines()
    assert "note on the standing approval: opened the panel capture" in out
    # The receipts land between the worker's report and the diff, where a reviewer
    # reads them before judging anything.
    assert out.index("captures and receipts") < out.index("1 file")


def test_review_show_survives_a_capture_path_that_is_gone(task, monkeypatch, capsys) -> None:
    _stub_bundle(monkeypatch)
    progress.record(task.id, phase="done", note="shots at /nowhere/at/all/captures", conn=task.conn)
    assert cli.main(["review", "show", str(task.id)]) == 0
    assert "/nowhere/at/all/captures — (not found)" in capsys.readouterr().out
