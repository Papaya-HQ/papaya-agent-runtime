"""A brief that opens with a heading names its own task; --title is then optional."""

from __future__ import annotations

import shutil
from collections import namedtuple

import pytest

from papaya_agent_runtime import cli, preflight

Usage = namedtuple("Usage", "total used free")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PPY_HOME", str(tmp_path / ".ppy"))
    (tmp_path / ".ppy").mkdir()
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(100e9, 20e9, 80e9))
    return tmp_path


@pytest.fixture
def fake_supervisor(monkeypatch):
    """Capture the packet a dispatch would send, without a supervisor."""
    sent: dict = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def dispatch_task(self, **kwargs):
            sent.update(kwargs)
            return {"ok": True, "task_id": 7, "run_id": 3, "branch": "ppy/task-7-abc"}

    import papaya_agent_runtime.supervisor.client as client_mod

    monkeypatch.setattr(client_mod, "SupervisorClient", FakeClient)
    return sent


def test_title_from_brief_takes_the_first_heading_whatever_its_depth() -> None:
    assert preflight.title_from_brief("# Ship the summary panel\n\nBody.\n") == (
        "Ship the summary panel"
    )
    assert preflight.title_from_brief("\n\n###   Fix   the   spacing  \n") == "Fix the spacing"
    assert preflight.title_from_brief("intro prose\n\n## Second line wins\n") == "Second line wins"


def test_title_from_brief_collapses_whitespace_and_caps_the_length() -> None:
    long_heading = "# " + " ".join(["word"] * 60)
    derived = preflight.title_from_brief(long_heading)
    assert derived is not None
    assert len(derived) == preflight.TITLE_CAP
    assert derived.startswith("word word")


def test_title_from_brief_is_none_when_there_is_no_heading() -> None:
    assert preflight.title_from_brief("no heading here\njust prose\n") is None
    assert preflight.title_from_brief("#\n#   \n") is None


def test_dispatch_without_title_derives_it_from_the_brief(home, fake_supervisor, capsys) -> None:
    brief = home / "brief.md"
    brief.write_text("#  Rebuild   the review surface \n\nWhy: it is unreadable.\n")
    rc = cli.main(["dispatch", "--repo", "papaya", "--brief", str(brief)])
    assert rc == 0
    assert fake_supervisor["title"] == "Rebuild the review surface"
    assert "Rebuild the review surface" in capsys.readouterr().out


def test_an_explicit_title_still_wins_over_the_brief(home, fake_supervisor) -> None:
    brief = home / "brief.md"
    brief.write_text("# Heading in the brief\n\nBody.\n")
    rc = cli.main(["dispatch", "--repo", "papaya", "--brief", str(brief), "--title", "Typed by me"])
    assert rc == 0
    assert fake_supervisor["title"] == "Typed by me"


def test_dispatch_with_neither_title_nor_brief_is_refused(home, monkeypatch, capsys) -> None:
    class Untouchable:
        def __init__(self, *a, **k):
            raise AssertionError("a nameless dispatch must not reach the supervisor")

    import papaya_agent_runtime.supervisor.client as client_mod

    monkeypatch.setattr(client_mod, "SupervisorClient", Untouchable)
    rc = cli.main(["dispatch", "--repo", "papaya", "--instructions", "go"])
    assert rc == 1
    assert "--title" in capsys.readouterr().err


def test_a_brief_with_no_heading_and_no_title_is_refused(home, monkeypatch, capsys) -> None:
    class Untouchable:
        def __init__(self, *a, **k):
            raise AssertionError("a nameless dispatch must not reach the supervisor")

    import papaya_agent_runtime.supervisor.client as client_mod

    monkeypatch.setattr(client_mod, "SupervisorClient", Untouchable)
    brief = home / "brief.md"
    brief.write_text("just prose, no heading at all\n")
    rc = cli.main(["dispatch", "--repo", "papaya", "--brief", str(brief)])
    assert rc == 1
    assert "no Markdown heading" in capsys.readouterr().err
