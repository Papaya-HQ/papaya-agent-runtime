"""Hermetic tests for durable instance memory under .ppy/memory/."""

from __future__ import annotations

import subprocess

import pytest

from papaya_agent_runtime import memory, repos
from papaya_agent_runtime.cli import main


@pytest.fixture
def ppy_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PPY_HOME", str(tmp_path / ".ppy"))
    return tmp_path / ".ppy"


def _make_source_repo(path) -> str:
    path.mkdir(parents=True)
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", *a], cwd=path, check=True, capture_output=True
    )
    run("init", "-q", "-b", "main")
    run("config", "user.name", "t")
    run("config", "user.email", "t@t")
    (path / "file.txt").write_text("hello\n")
    run("add", ".")
    run("commit", "-qm", "init")
    return str(path)


def test_ensure_layout_seeds_instance_tier(ppy_home) -> None:
    memory.ensure_memory_layout()
    assert memory.preferences_path().exists()
    assert memory.relationships_path().exists()
    assert memory.index_path().exists()
    assert memory.board_path().exists()
    assert memory.repos_root().is_dir()
    assert "# Preferences" in memory.preferences_path().read_text()
    assert "# Work board" in memory.board_path().read_text()


def test_worker_context_points_at_absolute_repo_memory(ppy_home) -> None:
    memory.seed_repo_memory("widgets")
    ctx = memory.worker_context("widgets")
    # Absolute paths so a worker (cwd = its worktree) can reach them.
    assert str(memory.repo_notes_path("widgets")) in ctx
    assert str(memory.repo_tasks_path("widgets")) in ctx
    assert memory.repo_notes_path("widgets").is_absolute()
    assert "progress" in ctx.lower()
    # Workers post their plan first so the manager can review/steer early.
    assert "plan" in ctx.lower()


def test_ensure_layout_is_idempotent_and_preserves_edits(ppy_home) -> None:
    memory.ensure_memory_layout()
    memory.preferences_path().write_text("# Preferences\n\n- likes terse updates\n")
    memory.ensure_memory_layout()  # must not clobber
    assert "likes terse updates" in memory.preferences_path().read_text()


def test_seed_repo_memory_creates_dir_with_notes_and_tasks(ppy_home) -> None:
    repo_dir = memory.seed_repo_memory("widgets", origin="https://x/widgets", default_branch="main")
    assert repo_dir.is_dir()
    notes = memory.repo_notes_path("widgets").read_text()
    assert "# widgets — notes" in notes
    assert "https://x/widgets" in notes
    assert "main" in notes
    assert memory.repo_tasks_path("widgets").exists()
    assert "# widgets — follow-ups" in memory.repo_tasks_path("widgets").read_text()


def test_seed_repo_memory_preserves_existing_learnings(ppy_home) -> None:
    memory.seed_repo_memory("widgets", origin="https://x/widgets", default_branch="main")
    notes = memory.repo_notes_path("widgets")
    notes.write_text(notes.read_text() + "\n- learned: run `make check`\n")
    memory.seed_repo_memory("widgets", origin="https://x/widgets", default_branch="main")
    assert "learned: run `make check`" in notes.read_text()  # existing note preserved


def test_add_repo_seeds_per_repo_memory(tmp_path, ppy_home) -> None:
    source = _make_source_repo(tmp_path / "source")
    repos.add_repo(source)
    assert memory.repo_dir("source").is_dir()
    assert "# source — notes" in memory.repo_notes_path("source").read_text()
    assert memory.repo_tasks_path("source").exists()


def test_cli_memory_path_instance_and_repo(tmp_path, ppy_home, capsys) -> None:
    assert main(["memory", "path"]) == 0
    assert str(memory.memory_dir()) in capsys.readouterr().out

    assert main(["memory", "path", "--repo", "svc"]) == 0
    assert str(memory.repo_dir("svc")) in capsys.readouterr().out


def test_cli_memory_show_instance_tier(tmp_path, ppy_home, capsys) -> None:
    assert main(["memory", "show"]) == 0
    out = capsys.readouterr().out
    assert "tasks.md" in out  # the manager's work board
    assert "preferences.md" in out
    assert "relationships.md" in out


def test_cli_memory_init_seeds_registered_repos(tmp_path, ppy_home, capsys) -> None:
    source = _make_source_repo(tmp_path / "svc")
    repos.add_repo(source)
    memory.repo_notes_path("svc").unlink()  # simulate a note that went missing
    assert main(["memory", "init"]) == 0
    assert "seeded" in capsys.readouterr().out
    assert memory.repo_notes_path("svc").exists()
