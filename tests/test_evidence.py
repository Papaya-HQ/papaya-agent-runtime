"""`ppy evidence add`: a worker may keep its own saved output, and nothing else.

Issue #127 is ten refused `cp` commands, every one a worker copying its own session's
saved tool output into its evidence directory. The refusals were right — `cp` takes any
path — so the answer is a narrower command, confined the way `--note-file` was confined
in PR #124 after review found it could read any file.

Every row of the confinement rule is a test here, and the negatives matter more than the
positive: an outside path, a `..`, a symlink out, another task's session, another
project's tool-results, a directory, a device, an oversize file, an overwrite.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from papaya_agent_runtime import evidence
from papaya_agent_runtime.cli import main
from papaya_agent_runtime.state import init_db, store

SESSION = "e016105b-c59d-4df4-9ca4-8772fea6f1eb"
OTHER_SESSION = "4d22ec0a-d766-48d1-8a4e-bc30a1de6766"


@pytest.fixture
def claude(tmp_path, monkeypatch) -> Path:
    """A Claude home whose `projects` directory this test owns."""
    home = tmp_path / "claude"
    (home / "projects").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    return home


def tool_results(claude: Path, session: str, *, project: str = "-wt-app") -> Path:
    where = claude / "projects" / project / session / evidence.TOOL_RESULTS
    where.mkdir(parents=True, exist_ok=True)
    return where


@pytest.fixture
def task(ppy_home, tmp_path):
    """A worker task with a worktree, a repository, and one recorded session."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    conn = init_db()
    repo_id = store.add_repo(
        conn,
        name="app",
        origin="git@example.com:app.git",
        local_path=str(tmp_path / "clone"),
        default_branch="main",
        base_sha=None,
    )
    run_id = store.create_run(conn, "ship it")
    task_id = store.add_task(conn, run_id=run_id, title="build", repo_id=repo_id)
    store.update_task_fields(conn, task_id, worktree_path=str(worktree))
    store.upsert_session(conn, task_id=task_id, provider="claude", provider_session_id=SESSION)
    conn.close()
    return task_id, worktree


def evidence_dir(worktree: Path) -> Path:
    return worktree / ".ppy-evidence"


# ── what may be kept ────────────────────────────────────────────────────────


def test_a_saved_tool_result_of_this_tasks_own_session_is_kept(task, claude) -> None:
    task_id, worktree = task
    source = tool_results(claude, SESSION) / "baukveark.txt"
    source.write_text("900 passed in 712.00s\n", encoding="utf-8")

    target = evidence.add(task_id, source, name="blocks-build.txt")

    assert target == evidence_dir(worktree) / "blocks-build.txt"
    assert target.read_text(encoding="utf-8") == "900 passed in 712.00s\n"


def test_a_session_recorded_only_in_the_tasks_events_counts_too(task, claude) -> None:
    """`sessions` keeps one row per task and overwrites it on resume; #127's task 180
    was refused a copy from a session that had scrolled out of that row."""
    task_id, _worktree = task
    conn = init_db()
    store.upsert_session(  # a resume: the row is overwritten
        conn, task_id=task_id, provider="claude", provider_session_id=OTHER_SESSION
    )
    store.append_event(
        conn,
        kind="worker_result",
        payload={"task_id": task_id, "session_id": SESSION},
        task_id=task_id,
    )
    conn.close()
    source = tool_results(claude, SESSION) / "older.txt"
    source.write_text("from the first session\n", encoding="utf-8")

    assert evidence.add(task_id, source).name == "older.txt"
    assert evidence.session_ids(init_db(), task_id) == {SESSION, OTHER_SESSION}


def test_a_file_in_the_tasks_own_worktree_is_kept(task, claude) -> None:
    task_id, worktree = task
    source = worktree / "gate.txt"
    source.write_text("green\n", encoding="utf-8")

    assert evidence.add(task_id, source, name="gate-local.txt").exists()


# ── what may not ────────────────────────────────────────────────────────────


def _refused(task_id: int, source, **kwargs) -> str:
    with pytest.raises(evidence.EvidenceError) as caught:
        evidence.add(task_id, source, **kwargs)
    return str(caught.value)


def test_a_path_outside_both_places_is_refused_by_name(task, claude, tmp_path) -> None:
    task_id, worktree = task
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY\n", encoding="utf-8")

    said = _refused(task_id, secret)

    assert str(secret) in said and "is not task" in said
    assert not evidence_dir(worktree).exists()  # nothing was even created


def test_a_dot_dot_out_of_the_tool_results_directory_is_refused(task, claude, tmp_path) -> None:
    task_id, _worktree = task
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY\n", encoding="utf-8")
    where = tool_results(claude, SESSION)
    reach = where / ".." / ".." / ".." / ".." / ".." / "id_rsa"
    assert reach.resolve() == secret.resolve()

    assert "is not task" in _refused(task_id, reach)


def test_a_symlink_out_of_the_tool_results_directory_is_refused(task, claude, tmp_path) -> None:
    task_id, _worktree = task
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY\n", encoding="utf-8")
    link = tool_results(claude, SESSION) / "innocent.txt"
    link.symlink_to(secret)

    # Resolution happens first and the RESOLVED path is what is judged, so a link
    # that lands outside is refused by the same rule as the path itself.
    said = _refused(task_id, link)
    assert str(secret.resolve()) in said


def test_a_symlink_out_of_the_worktree_is_refused(task, claude, tmp_path) -> None:
    task_id, worktree = task
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY\n", encoding="utf-8")
    link = worktree / "gate.txt"
    link.symlink_to(secret)

    assert "is not task" in _refused(task_id, link)


def test_another_tasks_session_is_refused(task, claude, ppy_home) -> None:
    task_id, _worktree = task
    conn = init_db()
    other = store.add_task(conn, run_id=store.create_run(conn, "other"), title="other")
    store.upsert_session(conn, task_id=other, provider="claude", provider_session_id=OTHER_SESSION)
    conn.close()
    source = tool_results(claude, OTHER_SESSION) / "theirs.txt"
    source.write_text("another worker's output\n", encoding="utf-8")

    assert "is not task" in _refused(task_id, source)


def test_another_projects_tool_results_is_refused_even_with_a_known_session(task, claude) -> None:
    """The session id is the authority, but the shape is checked too: only
    `<projects>/<project>/<session>/tool-results/<file>` is the session's own."""
    task_id, _worktree = task
    deeper = claude / "projects" / "-wt-app" / "nested" / SESSION / evidence.TOOL_RESULTS
    deeper.mkdir(parents=True)
    source = deeper / "elsewhere.txt"
    source.write_text("not where a session writes\n", encoding="utf-8")

    assert "is not task" in _refused(task_id, source)


def test_a_directory_a_device_and_a_missing_file_are_refused(task, claude) -> None:
    task_id, _worktree = task
    where = tool_results(claude, SESSION)
    (where / "subdir").mkdir()

    assert "not a regular file" in _refused(task_id, where / "subdir")
    assert "there is no file at" in _refused(task_id, where / "never-written.txt")
    # A device is not a receipt, however it is reached.
    assert "is not task" in _refused(task_id, "/dev/null")


def test_a_file_that_is_not_yours_is_refused(task, claude, monkeypatch) -> None:
    task_id, _worktree = task
    source = tool_results(claude, SESSION) / "theirs.txt"
    source.write_text("root's\n", encoding="utf-8")
    monkeypatch.setattr(os, "getuid", lambda: os.stat(source).st_uid + 1)

    assert "is not yours" in _refused(task_id, source)


def test_an_oversize_file_is_refused(task, claude, monkeypatch) -> None:
    task_id, _worktree = task
    source = tool_results(claude, SESSION) / "huge.txt"
    source.write_text("x" * 64, encoding="utf-8")
    monkeypatch.setattr(evidence, "MAX_BYTES", 8)

    assert "over the 8 byte limit" in _refused(task_id, source)


def test_a_name_that_could_traverse_is_refused(task, claude) -> None:
    task_id, _worktree = task
    source = tool_results(claude, SESSION) / "log.txt"
    source.write_text("ok\n", encoding="utf-8")

    for name in ("../escape.txt", "sub/dir.txt", "..", "", ".hidden", "x" * 200):
        assert "is not a name a receipt may have" in _refused(task_id, source, name=name)


def test_an_existing_receipt_is_never_replaced_without_force(task, claude) -> None:
    task_id, worktree = task
    source = tool_results(claude, SESSION) / "log.txt"
    source.write_text("second\n", encoding="utf-8")
    evidence_dir(worktree).mkdir()
    (evidence_dir(worktree) / "log.txt").write_text("first\n", encoding="utf-8")

    assert "pass --force to replace it" in _refused(task_id, source)
    assert (evidence_dir(worktree) / "log.txt").read_text(encoding="utf-8") == "first\n"

    evidence.add(task_id, source, force=True)
    assert (evidence_dir(worktree) / "log.txt").read_text(encoding="utf-8") == "second\n"


def test_a_task_with_no_worktree_has_nowhere_to_keep_evidence(ppy_home, claude) -> None:
    conn = init_db()
    task_id = store.add_task(conn, run_id=store.create_run(conn, "r"), title="build")
    conn.close()

    assert "has no worktree" in _refused(task_id, __file__)
    assert "does not exist" in _refused(task_id + 99, __file__)


def test_a_crafted_session_id_in_the_record_is_never_looked_for(task, claude, ppy_home) -> None:
    """Only ids of the recorded shape are read back: a payload is not a path."""
    task_id, _worktree = task
    conn = init_db()
    for crafted in ("../../..", "..", "*", ""):
        store.append_event(
            conn,
            kind="worker_result",
            payload={"task_id": task_id, "session_id": crafted},
            task_id=task_id,
        )
    conn.commit()

    assert evidence.session_ids(conn, task_id) == {SESSION}


# ── the command ─────────────────────────────────────────────────────────────


def test_the_command_names_the_task_and_never_the_working_directory(
    task, claude, capsys, monkeypatch, tmp_path
) -> None:
    task_id, worktree = task
    source = tool_results(claude, SESSION) / "ios-build.txt"
    source.write_text("** BUILD SUCCEEDED **\n", encoding="utf-8")
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert main(["evidence", "add", str(source), "--task", str(task_id), "--as", "ios.txt"]) == 0

    out = capsys.readouterr().out
    assert str(evidence_dir(worktree) / "ios.txt") in out
    assert (evidence_dir(worktree) / "ios.txt").exists()


def test_the_command_refuses_by_name_and_exits_one(task, claude, capsys, tmp_path) -> None:
    task_id, _worktree = task
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY\n", encoding="utf-8")

    assert main(["evidence", "add", str(secret), "--task", str(task_id)]) == 1

    assert "refused:" in capsys.readouterr().err
