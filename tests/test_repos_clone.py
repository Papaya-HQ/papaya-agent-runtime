"""The one clone path: visible start, progress and end, and prompt, clean failure.

The forge is always a local repository; nothing here touches the network.
"""

from __future__ import annotations

import os
import stat
import subprocess
import time

import pytest

from papaya_agent_runtime import repos
from test_repos import _make_source_repo


@pytest.fixture
def ppy_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PPY_HOME", str(tmp_path / ".ppy"))
    return tmp_path / ".ppy"


def _lines() -> tuple[list[str], repos.ProgressSink]:
    lines: list[str] = []
    return lines, lines.append


def _fake_git(tmp_path, monkeypatch, body: str) -> None:
    """A `git` first on PATH; `git config` answers empty so only `clone` runs the body."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    script = bin_dir / "git"
    script.write_text(f'#!/bin/sh\nif [ "$1" = config ]; then exit 1; fi\n{body}\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def test_a_clone_reports_start_and_completion_and_matches_a_plain_clone(
    tmp_path,
    ppy_home,
    capsys,
) -> None:
    source = _make_source_repo(tmp_path / "source")
    lines, sink = _lines()
    dest = tmp_path / "dest"

    repos.clone_repo(source, dest, "source", sink, tty=False)

    assert lines[0] == f"cloning source from {source}"
    assert lines[-1].startswith("cloned source in ") and lines[-1].endswith("s")
    assert (dest / "file.txt").read_text() == "hello\n"
    origin = subprocess.run(
        ["git", "-C", str(dest), "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert origin == source
    captured = capsys.readouterr()
    assert captured.out == ""  # progress never reaches stdout


def test_non_terminal_progress_is_plain_lines_with_phase_and_percentage(tmp_path) -> None:
    source = _make_source_repo(tmp_path / "source")
    lines, sink = _lines()

    repos.clone_repo(f"file://{source}", tmp_path / "dest", "source", sink, tty=False)

    progress = [ln for ln in lines if "%" in ln]
    assert progress, lines
    assert any("Receiving objects 100%" in ln for ln in progress)
    assert all("\r" not in ln and "\n" not in ln for ln in lines)


def test_the_throttle_writes_a_handful_of_lines_for_a_large_clone() -> None:
    now = [0.0]
    throttle = repos._ProgressThrottle(clock=lambda: now[0])
    admitted = []
    for phase in ("Receiving objects", "Resolving deltas"):
        for pct in range(0, 101):
            now[0] += 0.05  # a fast clone: each phase over in five seconds
            if throttle.admit(phase, pct):
                admitted.append((phase, pct))
    # A line on each phase's first record and at 100%, nothing in between while fast.
    assert admitted == [
        ("Receiving objects", 0),
        ("Receiving objects", 100),
        ("Resolving deltas", 0),
        ("Resolving deltas", 100),
    ]

    # A slow phase gets a line every 10 points, once 15s have also passed.
    slow = repos._ProgressThrottle(clock=lambda: now[0])
    seen = []
    for pct in range(0, 100):
        now[0] += 5.0
        if slow.admit("Receiving objects", pct):
            seen.append(pct)
    assert seen[0] == 0 and all(b - a >= 10 for a, b in zip(seen, seen[1:], strict=False))
    assert len(seen) <= 10


def test_a_terminal_gets_git_progress_raw_and_no_parsed_lines(tmp_path, capfd) -> None:
    source = _make_source_repo(tmp_path / "source")
    lines, sink = _lines()

    repos.clone_repo(f"file://{source}", tmp_path / "dest", "source", sink, tty=True)

    assert not any("%" in ln for ln in lines)
    assert "Receiving objects" in capfd.readouterr().err


def test_a_failing_clone_raises_with_gits_text_and_leaves_nothing(tmp_path, ppy_home) -> None:
    dest = tmp_path / "dest"
    with pytest.raises(repos.RepoError) as caught:
        repos.clone_repo(str(tmp_path / "nowhere"), dest, "nowhere", tty=False)
    assert "does not exist" in str(caught.value) or "not a git repository" in str(caught.value)
    assert not dest.exists()


def test_add_repo_with_no_sink_and_no_logging_writes_start_and_done_to_stderr(
    tmp_path,
    ppy_home,
    capfd,
) -> None:
    source = _make_source_repo(tmp_path / "source")

    repos.add_repo(source)

    captured = capfd.readouterr()
    assert f"cloning source from {source}" in captured.err
    assert "cloned source in " in captured.err
    assert captured.out == ""


def test_the_default_sink_uses_the_logger_when_it_is_enabled_for_info(
    tmp_path,
    ppy_home,
    caplog,
    capfd,
) -> None:
    source = _make_source_repo(tmp_path / "source")

    with caplog.at_level("INFO", logger="papaya_agent_runtime.repos"):
        repos.add_repo(source)

    assert any("cloned source in " in r.getMessage() for r in caplog.records)
    assert "cloned source in " not in capfd.readouterr().err


def test_add_repo_keeps_its_message_and_the_destination_is_free_to_retry(
    tmp_path,
    ppy_home,
) -> None:
    source = _make_source_repo(tmp_path / "source")
    broken = tmp_path / "gone"
    with pytest.raises(repos.RepoError, match="could not clone gone from its forge"):
        repos.add_repo(str(broken), forge_url=str(tmp_path / "missing-forge"))
    assert not (repos.repos_dir() / "gone").exists()
    assert repos.add_repo(source).name == "source"


def test_credentials_in_a_url_never_reach_a_line_or_an_error(tmp_path, ppy_home) -> None:
    url = f"file://user:s3cret@{tmp_path}/nowhere"
    lines, sink = _lines()
    with pytest.raises(repos.RepoError) as caught:
        repos.clone_repo(url, tmp_path / "dest", "nowhere", sink, tty=False)
    assert "s3cret" not in str(caught.value)
    assert all("s3cret" not in ln for ln in lines)
    assert lines[0].startswith("cloning nowhere from file://")

    with pytest.raises(repos.RepoError) as wrapped:
        repos.add_repo("nowhere", forge_url=url)
    assert "s3cret" not in str(wrapped.value)


def test_a_clone_that_goes_silent_is_killed_even_if_a_child_holds_stderr(
    tmp_path,
    monkeypatch,
    ppy_home,
) -> None:
    # The background sleep inherits git's stderr and outlives it once git is killed.
    _fake_git(tmp_path, monkeypatch, "sleep 30 &\nsleep 30")
    monkeypatch.setenv(repos.CLONE_STALL_ENV, "0.6")
    dest = tmp_path / "dest"
    lines, sink = _lines()

    started = time.monotonic()
    with pytest.raises(repos.RepoError, match=r"clone of stuck stalled: git wrote nothing"):
        repos.clone_repo("https://example.invalid/x.git", dest, "stuck", sink, tty=False)

    assert time.monotonic() - started < 8
    assert not dest.exists()


def test_a_stalled_clone_never_removes_a_directory_that_existed_before(
    tmp_path, monkeypatch
) -> None:
    _fake_git(tmp_path, monkeypatch, "sleep 30 &\nsleep 30")
    monkeypatch.setenv(repos.CLONE_STALL_ENV, "0.6")
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "mine.txt").write_text("keep")
    with pytest.raises(repos.RepoError):
        repos.clone_repo("https://example.invalid/x.git", dest, "stuck", tty=False)
    assert (dest / "mine.txt").read_text() == "keep"


def test_an_interrupted_clone_leaves_no_directory(tmp_path, monkeypatch) -> None:
    _fake_git(tmp_path, monkeypatch, 'mkdir -p "$4"\nsleep 30')
    dest = tmp_path / "dest"

    class Interrupted(BaseException):
        pass

    class InterruptedPopen(subprocess.Popen):
        def wait(self, timeout=None):
            if timeout is not None:
                time.sleep(0.3)  # let the fake git create dest first
                raise Interrupted
            return super().wait()

    monkeypatch.setattr(repos.subprocess, "Popen", InterruptedPopen)
    with pytest.raises(Interrupted):
        repos.clone_repo("https://example.invalid/x.git", dest, "x", tty=False)
    assert not dest.exists()


def test_non_terminal_clones_refuse_prompts_and_respect_a_users_ssh_command(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty-gitconfig"))
    env = repos._non_interactive_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"

    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i mine")
    assert repos._non_interactive_env()["GIT_SSH_COMMAND"] == "ssh -i mine"

    monkeypatch.delenv("GIT_SSH_COMMAND")
    (tmp_path / "gitconfig").write_text("[core]\n\tsshCommand = ssh -i configured\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    assert "GIT_SSH_COMMAND" not in repos._non_interactive_env()
