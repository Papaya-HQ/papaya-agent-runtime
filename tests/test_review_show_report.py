"""`ppy review show` puts the worker's own report, in full, above the diff."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import make_git_repo
from papaya_agent_runtime import cli, migrations, progress
from papaya_agent_runtime.config import MMConfig, save_config
from papaya_agent_runtime.state import init_db, store


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PPY_HOME", str(tmp_path / ".ppy"))
    return init_db()


_MIGRATION = '''"""a revision"""

revision = "{revision}"
down_revision = "{down_revision}"


def upgrade() -> None:
    pass
'''


def _git(path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _lease_worktree(conn, clone, task_id: int) -> str:
    """A real lease worktree of the base clone, on the task's own branch, recorded as active."""
    branch = f"ppy/task-{task_id}-abc"
    path = Path(clone).parent / f"slot-{task_id}"
    _git(clone, "worktree", "add", "-q", "-b", branch, str(path), "HEAD")
    store.add_lease(
        conn,
        lease_id=f"lease-{task_id}",
        repo_id=1,
        task_id=task_id,
        branch=branch,
        worktree_path=str(path),
        base_sha=_git(clone, "rev-parse", "HEAD"),
        backend="git",
    )
    store.update_task_fields(
        conn,
        task_id,
        worktree_path=str(path),
        base_sha=_git(clone, "rev-parse", "HEAD"),
        branch=branch,
        lease_id=f"lease-{task_id}",
    )
    return str(path)


def _add_migration(worktree, *, revision: str, down_revision: str) -> None:
    migration = Path(worktree) / "backend" / "alembic" / "versions" / f"{revision}.py"
    migration.parent.mkdir(parents=True, exist_ok=True)
    migration.write_text(_MIGRATION.format(revision=revision, down_revision=down_revision))
    _git(worktree, "add", ".")
    _git(
        worktree,
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@e.com",
        "commit",
        "-qm",
        f"add revision {revision}",
    )


def _two_tasks_each_adding_a_migration(conn, tmp_path, *, first_parent: str, second_parent: str):
    clone = make_git_repo(tmp_path / "clone")
    repo_id = store.add_repo(
        conn,
        name="backend",
        origin=str(tmp_path / "origin"),
        local_path=clone,
        default_branch="main",
        base_sha=None,
    )
    run_id = store.create_run(conn, "two migrations")
    ids = []
    for title, revision, parent in (
        ("add the events table", "aaa", first_parent),
        ("add the sessions table", "bbb", second_parent),
    ):
        task_id = store.add_task(conn, run_id=run_id, title=title, repo_id=repo_id)
        worktree = _lease_worktree(conn, clone, task_id)
        _add_migration(worktree, revision=revision, down_revision=parent)
        store.set_task_status(conn, task_id, "in_progress")
        ids.append(task_id)
    return ids


def test_review_show_prints_the_full_latest_report_first(home, monkeypatch, capsys) -> None:
    conn = home
    run_id = store.create_run(conn, "ship it")
    task_id = store.add_task(conn, run_id=run_id, title="build")
    long_note = "Verification:\n" + "\n".join(f"- check {i}: PASS" for i in range(40))
    progress.record(task_id, phase="implement", note="halfway", conn=conn)
    progress.record(task_id, phase="done", note=long_note, conn=conn)

    import papaya_agent_runtime.review as review_mod

    monkeypatch.setattr(
        review_mod,
        "build_bundle",
        lambda tid: SimpleNamespace(
            task_id=tid, base_sha="a" * 40, head_sha="b" * 40, files_changed=2, diffstat="2 files"
        ),
    )
    assert cli.main(["review", "show", str(task_id)]) == 0
    out = capsys.readouterr().out
    assert "worker report (done, 2 report(s)" in out, out
    assert out.index("worker report (done, 2 report(s)") < out.index("2 files")
    assert "- check 39: PASS" in out  # the whole note, not a truncated line
    assert f"ppy progress {task_id}" in out


def test_review_show_prints_the_task_usage_advisory(home, monkeypatch, capsys) -> None:
    cfg = MMConfig()
    cfg.usage.input_ceiling_per_task = 10
    save_config(cfg)
    conn = home
    run_id = store.create_run(conn, "ship it")
    task_id = store.add_task(conn, run_id=run_id, title="expensive task")
    store.record_usage(
        conn,
        run_id=run_id,
        task_id=task_id,
        provider="codex",
        model="m",
        reasoning="low",
        input_tokens=11,
        output_tokens=1,
    )
    import papaya_agent_runtime.review as review_mod

    monkeypatch.setattr(
        review_mod,
        "build_bundle",
        lambda tid: SimpleNamespace(
            task_id=tid, base_sha="a" * 40, head_sha="b" * 40, files_changed=0, diffstat=""
        ),
    )
    assert cli.main(["review", "show", str(task_id)]) == 0
    assert "usage advisory" in capsys.readouterr().out


def test_review_show_flags_two_unmerged_migrations_off_the_same_parent(
    home, tmp_path, capsys
) -> None:
    """The two-heads condition itself: both diffs exist and both name one down_revision."""
    first, second = _two_tasks_each_adding_a_migration(
        home, tmp_path, first_parent="base", second_parent="base"
    )
    assert cli.main(["review", "show", str(second)]) == 0
    out = capsys.readouterr().out
    assert "MIGRATION COLLISION" in out, out
    assert f'task {first} "add the events table"' in out
    assert "backend/alembic/versions/aaa.py" in out
    assert "backend/alembic/versions/bbb.py" in out
    assert "'base'" in out
    assert out.index("MIGRATION COLLISION") < out.index(f"task {second}: ")


def test_review_show_says_nothing_when_the_two_migrations_have_different_parents(
    home, tmp_path, capsys
) -> None:
    _first, second = _two_tasks_each_adding_a_migration(
        home, tmp_path, first_parent="base", second_parent="aaa"
    )
    assert cli.main(["review", "show", str(second)]) == 0
    assert "MIGRATION COLLISION" not in capsys.readouterr().out


def test_review_show_states_the_merge_order_for_a_stacked_layer(home, monkeypatch, capsys) -> None:
    """Task 92: 'a line about merge order would have saved me discovering it at the end'."""
    conn = home
    run_id = store.create_run(conn, "stack")
    bottom = store.add_task(conn, run_id=run_id, title="bottom layer")
    store.update_task_fields(conn, bottom, branch="ppy/task-1-aaa")
    top = store.add_task(conn, run_id=run_id, title="top layer")
    store.update_task_fields(
        conn, top, branch="ppy/task-2-bbb", stacked_on="ppy/task-1-aaa", stacked_on_task=bottom
    )

    import papaya_agent_runtime.review as review_mod

    monkeypatch.setattr(
        review_mod,
        "build_bundle",
        lambda tid: SimpleNamespace(
            task_id=tid, base_sha="a" * 40, head_sha="b" * 40, files_changed=1, diffstat="1 file"
        ),
    )
    assert cli.main(["review", "show", str(top)]) == 0
    out = capsys.readouterr().out
    assert "stack: layer 2 of 2; its pull request targets ppy/task-1-aaa" in out, out
    assert f'task {bottom} "bottom layer" (branch ppy/task-1-aaa, unmerged)' in out
    assert f"then this task {top}" in out
    assert out.index("stack: layer") < out.index(f"task {top}: ")

    # The bottom layer has no parent: nothing to sequence, so nothing is printed.
    assert cli.main(["review", "show", str(bottom)]) == 0
    assert "stack: layer" not in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Ownership is by commit, not by path (issue #74); in flight is bounded (issue #56)
# --------------------------------------------------------------------------- #


def _historical_task(conn, *, title: str, status: str, worktree: str, branch: str, merged=False):
    """A finished task whose record still points at a slot somebody else holds now."""
    run_id = store.create_run(conn, title)
    task_id = store.add_task(conn, run_id=run_id, title=title, repo_id=1)
    store.update_task_fields(
        conn, task_id, worktree_path=worktree, branch=branch, base_sha="0" * 40, lease_id=None
    )
    if merged:
        store.update_task_fields(conn, task_id, merged_sha="f" * 40, merged_at="2026-08-01T00:00Z")
    store.set_task_status(conn, task_id, status)
    return task_id


def test_recycled_slot_paths_do_not_turn_one_migration_into_phantom_competitors(
    home, tmp_path, capsys
) -> None:
    """Task 170's own migration was reported as colliding with tasks 4, 80 and 90:
    all three records pointed at the pool slot task 170 now held (2026-09-07)."""
    conn = home
    clone = make_git_repo(tmp_path / "clone")
    store.add_repo(
        conn,
        name="backend",
        origin=str(tmp_path / "origin"),
        local_path=clone,
        default_branch="main",
        base_sha=None,
    )
    run_id = store.create_run(conn, "radar")
    current = store.add_task(conn, run_id=run_id, title="radar engine foundation", repo_id=1)
    slot = _lease_worktree(conn, clone, current)
    _add_migration(slot, revision="d8f0a2c4", down_revision="c7e9a1b3")
    store.set_task_status(conn, current, "worker_done")

    # Three finished tasks whose records name the same slot, with branches that
    # are retired (the base clone never had them) — the shape in the report.
    for title, status, merged in (
        ("old failed", "failed", False),
        ("old delivered", "delivered", True),
        ("old delivered too", "delivered", True),
    ):
        _historical_task(
            conn, title=title, status=status, worktree=slot, branch=f"ppy/{title}", merged=merged
        )

    assert cli.main(["review", "show", str(current)]) == 0
    out = capsys.readouterr().out
    assert "MIGRATION COLLISION" not in out, out
    assert "MIGRATION CHECK INCOMPLETE" not in out, out


def test_an_unmerged_delivered_task_at_a_recycled_path_is_checked_by_its_branch(
    home, tmp_path, capsys
) -> None:
    """The positive case beside the phantom one: a real second branch off the same
    parent is still found, from its commits, after its slot was handed on."""
    conn = home
    first, second = _two_tasks_each_adding_a_migration(
        home, tmp_path, first_parent="base", second_parent="base"
    )
    # The first task delivered (PR open, unmerged) and its slot was pruned: the
    # lease is released, the directory is gone, the branch survives in the clone.
    clone = str(tmp_path / "clone")
    first_row, second_row = store.get_task(conn, first), store.get_task(conn, second)
    store.release_lease(conn, first_row["lease_id"])
    _git(clone, "worktree", "remove", "--force", first_row["worktree_path"])
    store.set_task_status(conn, first, "delivered")
    # And the second task's checkout now lives at the very path the first recorded.
    _git(clone, "worktree", "move", second_row["worktree_path"], first_row["worktree_path"])
    store.update_task_fields(conn, second, worktree_path=first_row["worktree_path"])
    conn.execute(
        "UPDATE leases SET worktree_path = ? WHERE id = ?",
        (first_row["worktree_path"], second_row["lease_id"]),
    )
    conn.commit()

    assert cli.main(["review", "show", str(second)]) == 0
    out = capsys.readouterr().out
    assert "MIGRATION COLLISION" in out, out
    assert f'task {first} "add the events table"' in out


def test_a_failed_task_is_not_in_flight_however_recent(home, tmp_path, capsys) -> None:
    """Tasks 2–5 (issue #56) were `failed` Codex runs with real migrations on their
    branches; they suggested `--stack-on` a month after anyone had looked at them."""
    conn = home
    first, second = _two_tasks_each_adding_a_migration(
        home, tmp_path, first_parent="base", second_parent="base"
    )
    store.set_task_status(conn, first, "failed")
    assert cli.main(["review", "show", str(second)]) == 0
    assert "MIGRATION COLLISION" not in capsys.readouterr().out
    assert migrations.dispatch_advisory(conn, store.get_repo(conn, "backend")) is not None
    assert f"--stack-on {first}" not in (
        migrations.dispatch_advisory(conn, store.get_repo(conn, "backend")) or ""
    )


def test_an_in_flight_task_nobody_has_touched_for_a_month_is_not_in_flight(home, tmp_path) -> None:
    conn = home
    first, _second = _two_tasks_each_adding_a_migration(
        home, tmp_path, first_parent="base", second_parent="base"
    )
    repo = store.get_repo(conn, "backend")
    assert f"--stack-on {first}" in (migrations.dispatch_advisory(conn, repo) or "")

    conn.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?", ("2026-08-01T00:00:00+00:00", first)
    )
    conn.commit()
    advisory = migrations.dispatch_advisory(conn, repo) or ""
    assert f"--stack-on {first}" not in advisory

    # A live runner is proof of life whatever the timestamp says.
    store.register_runner(conn, runner_id="r1", task_id=first, provider="fake")
    store.update_runner(conn, "r1", status="running")
    assert f"--stack-on {first}" in (migrations.dispatch_advisory(conn, repo) or "")


def test_review_names_an_in_flight_task_whose_commits_cannot_be_read(
    home, tmp_path, capsys
) -> None:
    """Missing or retired branches are said out loud, never silently passed."""
    conn = home
    first, second = _two_tasks_each_adding_a_migration(
        home, tmp_path, first_parent="base", second_parent="base"
    )
    first_row = store.get_task(conn, first)
    store.release_lease(conn, first_row["lease_id"])
    clone = str(tmp_path / "clone")
    _git(clone, "worktree", "remove", "--force", first_row["worktree_path"])
    _git(clone, "branch", "-D", first_row["branch"])

    assert cli.main(["review", "show", str(second)]) == 0
    out = capsys.readouterr().out
    assert "MIGRATION COLLISION" not in out
    assert "MIGRATION CHECK INCOMPLETE" in out, out
    assert f'task {first} "add the events table"' in out
    assert "exists neither in an owned worktree nor in the base clone" in out
