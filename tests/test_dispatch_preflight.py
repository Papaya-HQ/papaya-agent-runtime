"""Dispatch preflight: empty briefs and full disks are refused before a task exists."""

from __future__ import annotations

import shutil
from collections import namedtuple

import pytest

from papaya_agent_runtime import cli, health, preflight

Usage = namedtuple("Usage", "total used free")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PPY_HOME", str(tmp_path / ".ppy"))
    (tmp_path / ".ppy").mkdir()
    return tmp_path


def test_read_brief_refuses_missing_and_empty_files(home) -> None:
    with pytest.raises(preflight.PreflightError, match="not found"):
        preflight.read_brief(home / "nope.md")
    empty = home / "empty.md"
    empty.write_text("   \n\n")
    with pytest.raises(preflight.PreflightError, match="empty"):
        preflight.read_brief(empty)
    real = home / "brief.md"
    real.write_text("# Do the thing\n\nExactly this.\n")
    assert preflight.read_brief(real).startswith("# Do the thing")


def test_check_disk_refuses_below_the_floor(home, monkeypatch) -> None:
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(100e9, 99.7e9, 0.3e9))
    with pytest.raises(preflight.PreflightError, match="0.3 GB free"):
        preflight.check_disk(floor_gb=5)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(100e9, 20e9, 80e9))
    assert preflight.check_disk(floor_gb=5) == pytest.approx(80.0)


def test_floor_is_env_overridable(monkeypatch) -> None:
    monkeypatch.delenv(preflight.MIN_FREE_GB_ENV, raising=False)
    assert preflight.min_free_gb() == preflight.DEFAULT_MIN_FREE_GB
    monkeypatch.setenv(preflight.MIN_FREE_GB_ENV, "12")
    assert preflight.min_free_gb() == 12.0
    monkeypatch.setenv(preflight.MIN_FREE_GB_ENV, "garbage")
    assert preflight.min_free_gb() == preflight.DEFAULT_MIN_FREE_GB


def test_archive_brief_keeps_the_exact_packet(home) -> None:
    path = preflight.archive_brief("papaya", 42, "the brief\n")
    assert path == home / ".ppy" / "briefs" / "papaya" / "task-42.md"
    assert path.read_text() == "the brief\n"


def test_cli_dispatch_refuses_an_empty_brief_without_touching_the_supervisor(
    home, monkeypatch, capsys
) -> None:
    class Untouchable:
        def __init__(self, *a, **k):
            raise AssertionError("the supervisor must not be contacted on a refused preflight")

    import papaya_agent_runtime.supervisor.client as client_mod

    monkeypatch.setattr(client_mod, "SupervisorClient", Untouchable)
    empty = home / "empty.md"
    empty.write_text("\n")
    rc = cli.main(["dispatch", "--repo", "r", "--title", "t", "--brief", str(empty)])
    assert rc == 1
    assert "brief file is empty" in capsys.readouterr().err


def test_cli_dispatch_refuses_a_full_disk(home, monkeypatch, capsys) -> None:
    class Untouchable:
        def __init__(self, *a, **k):
            raise AssertionError("no dispatch on a full disk")

    import papaya_agent_runtime.supervisor.client as client_mod

    monkeypatch.setattr(client_mod, "SupervisorClient", Untouchable)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(100e9, 99.9e9, 0.1e9))
    rc = cli.main(["dispatch", "--repo", "r", "--title", "t", "--instructions", "go"])
    assert rc == 1
    assert "GB free" in capsys.readouterr().err


def test_cli_dispatch_runs_the_capacity_gate_before_contacting_the_supervisor(
    home, monkeypatch, capsys
) -> None:
    class Untouchable:
        def __init__(self, *a, **k):
            raise AssertionError("no dispatch when the worktree pool is full")

    import papaya_agent_runtime.supervisor.client as client_mod

    monkeypatch.setattr(client_mod, "SupervisorClient", Untouchable)
    monkeypatch.setattr(
        health,
        "require_dispatch_capacity",
        lambda repo: (_ for _ in ()).throw(
            health.DispatchHealthError(
                "the worktree pool has no free slot; run `ppy worktree prune`"
            )
        ),
    )
    rc = cli.main(["dispatch", "--repo", "r", "--title", "t", "--instructions", "go"])
    assert rc == 1
    assert "ppy worktree prune" in capsys.readouterr().err


def test_cli_dispatch_archives_the_brief_it_sent(home, monkeypatch, capsys) -> None:
    sent: dict = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def dispatch_task(self, **kwargs):
            sent.update(kwargs)
            return {"ok": True, "task_id": 7, "run_id": 3, "branch": "ppy/task-7-abc"}

    import papaya_agent_runtime.supervisor.client as client_mod

    monkeypatch.setattr(client_mod, "SupervisorClient", FakeClient)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(100e9, 20e9, 80e9))
    brief = home / "brief.md"
    brief.write_text("# Build it\n")
    rc = cli.main(["dispatch", "--repo", "papaya", "--title", "t", "--brief", str(brief)])
    out = capsys.readouterr().out
    assert rc == 0
    assert sent["instructions"] == "# Build it\n"
    archived = home / ".ppy" / "briefs" / "papaya" / "task-7.md"
    assert archived.read_text() == "# Build it\n"
    assert "brief archived" in out


def test_cli_rejects_brief_and_instructions_together(home, capsys) -> None:
    brief = home / "brief.md"
    brief.write_text("x\n")
    rc = cli.main(
        ["dispatch", "--repo", "r", "--title", "t", "--brief", str(brief), "--instructions", "y"]
    )
    assert rc == 1
    assert "not both" in capsys.readouterr().err
