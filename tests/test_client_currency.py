"""The person's own `papaya-agent`: kept at the version this runtime locks, and run on a
terminal of its own so it can draw its arrow-key pickers.

On 2026-09-24 `ppy setup` on runtime main (client pin 0.18.1) still showed numbered
prompts and the client's HTTP log lines: the client's stdout was a pipe, and the
client on the PATH was a uv tool pinned at 0.17.0 that nothing ever upgraded.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from papaya_agent_runtime import papaya

LOCKED = "0.18.2"
CLIENT = "/Users/someone/.local/bin/papaya-agent"


@pytest.fixture
def checkout(tmp_path) -> Path:
    """A runtime checkout whose uv.lock locks the client at :data:`LOCKED`."""
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "uv.lock").write_text(
        "version = 1\n\n"
        "[[package]]\n"
        'name = "httpx"\n'
        'version = "0.28.1"\n\n'
        "[[package]]\n"
        f'name = "{papaya.CLIENT_PACKAGE}"\n'
        f'version = "{LOCKED}"\n',
        encoding="utf-8",
    )
    return root


class FakeClient:
    """Answers the argv `keep_client_current` runs: the client, `uv tool list`, the reinstall."""

    def __init__(self, version: str | None, *, flag: bool = True, reinstall: int = 0):
        self.version = version
        self.flag = flag
        self.reinstall = reinstall
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, timeout, env=None, cwd=None):
        self.calls.append(list(argv))
        done = subprocess.CompletedProcess
        if argv == [CLIENT, "--version"]:
            if self.flag and self.version:
                return done(argv, 0, f"papaya-agent {self.version}\n", "")
            return done(argv, 2, "", "papaya-agent: error: unrecognized arguments: --version\n")
        if argv == ["uv", "tool", "list"]:
            listed = f"{papaya.CLIENT_PACKAGE} v{self.version}\n- papaya-agent\n"
            return done(argv, 0, listed if self.version else "", "")
        if argv[:3] == ["uv", "tool", "install"]:
            if self.reinstall == 0:
                self.version = argv[-1].split("==")[-1]
            return done(argv, self.reinstall, "", "error: network unreachable\n")
        raise AssertionError(f"unexpected command {argv}")


@pytest.fixture
def on_path(monkeypatch):
    monkeypatch.setattr(papaya, "installed", lambda: CLIENT)


REINSTALL = ["uv", "tool", "install", "--force", f"{papaya.CLIENT_PACKAGE}=={LOCKED}"]


# ── the locked version ──────────────────────────────────────────────────────


def test_the_locked_version_is_read_from_uv_lock(checkout) -> None:
    assert papaya.locked_client_version(checkout) == LOCKED


def test_without_a_lock_the_pyproject_floor_is_the_locked_version(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        f'dependencies = [\n    "{papaya.CLIENT_PACKAGE}>=0.18.2,<0.19.0",\n]\n', encoding="utf-8"
    )
    assert papaya.locked_client_version(tmp_path) == "0.18.2"


def test_this_checkout_locks_the_client_it_embeds() -> None:
    from importlib.metadata import version

    assert papaya.locked_client_version() == version(papaya.CLIENT_PACKAGE)


# ── the cases ───────────────────────────────────────────────────────────────


def test_an_absent_client_is_left_to_connect(checkout, monkeypatch) -> None:
    monkeypatch.setattr(papaya, "installed", lambda: None)
    client = FakeClient(None)
    result = papaya.keep_client_current(root=checkout, run=client)
    assert result == {"state": "absent", "line": None}
    assert client.calls == []


def test_an_older_client_is_reinstalled_at_the_locked_version(checkout, on_path) -> None:
    client = FakeClient("0.17.0")
    result = papaya.keep_client_current(root=checkout, run=client)
    assert result["state"] == "updated"
    assert result["line"] == "Updated papaya-agent 0.17.0 → 0.18.2"
    assert REINSTALL in client.calls


def test_a_client_without_a_version_flag_is_read_from_uv_tool_list(checkout, on_path) -> None:
    """0.18 and older refuse `--version`: that is how the 0.17.0 on the PATH answers."""
    client = FakeClient("0.17.0", flag=False)
    result = papaya.keep_client_current(root=checkout, run=client)
    assert result["before"] == "0.17.0"
    assert result["state"] == "updated"
    assert client.calls[:2] == [[CLIENT, "--version"], ["uv", "tool", "list"]]


def test_a_current_client_is_left_alone(checkout, on_path) -> None:
    client = FakeClient(LOCKED)
    result = papaya.keep_client_current(root=checkout, run=client)
    assert result["state"] == "current" and result["line"] is None
    assert not any(call[:3] == ["uv", "tool", "install"] for call in client.calls)


def test_a_newer_client_is_left_alone(checkout, on_path) -> None:
    client = FakeClient("0.19.2")
    result = papaya.keep_client_current(root=checkout, run=client)
    assert result["state"] == "newer" and result["line"] is None
    assert not any(call[:3] == ["uv", "tool", "install"] for call in client.calls)


def test_a_failed_reinstall_names_the_command_to_run(checkout, on_path) -> None:
    client = FakeClient("0.17.0", reinstall=1)
    result = papaya.keep_client_current(root=checkout, run=client)
    assert result["state"] == "failed"
    assert result["command"] == REINSTALL
    assert result["line"] == (
        "papaya-agent 0.17.0 is older than 0.18.2 and could not be updated: "
        f"run uv tool install --force {papaya.CLIENT_PACKAGE}==0.18.2"
    )


def test_a_client_that_will_not_say_its_version_is_left_alone(checkout, on_path) -> None:
    client = FakeClient(None, flag=False)
    result = papaya.keep_client_current(root=checkout, run=client)
    assert result["state"] == "unknown" and result["line"] is None
    assert not any(call[:3] == ["uv", "tool", "install"] for call in client.calls)


def test_a_reinstall_that_cannot_run_at_all_is_a_failure_not_a_crash(checkout, on_path) -> None:
    client = FakeClient("0.17.0")

    def run(argv, **kw):
        if argv[:3] == ["uv", "tool", "install"]:
            raise subprocess.TimeoutExpired(argv, 120)
        return client(argv, **kw)

    result = papaya.keep_client_current(root=checkout, run=run)
    assert result["state"] == "failed"


# ── doctor ──────────────────────────────────────────────────────────────────


def test_doctor_updates_an_older_client_and_says_so(on_path, monkeypatch, tmp_path) -> None:
    from papaya_agent_runtime.setup import doctor

    monkeypatch.setenv("PPY_HOME", str(tmp_path / ".ppy"))
    client = FakeClient("0.17.0", flag=False)
    real_run = papaya._run

    def run(argv, **kw):
        if argv[0] in (CLIENT, "uv"):
            return client(argv, **kw)
        return real_run(argv, **kw)

    monkeypatch.setattr(papaya, "_run", run)
    locked = papaya.locked_client_version()
    text = doctor.run_doctor()
    assert (
        f"Updated papaya-agent 0.17.0 → {locked}"
        in text.splitlines()[
            next(i for i, line in enumerate(text.splitlines()) if line.startswith("papaya:")) + 1
        ]
    )
    assert [
        "uv",
        "tool",
        "install",
        "--force",
        f"{papaya.CLIENT_PACKAGE}=={locked}",
    ] in client.calls


def test_doctor_names_the_command_when_the_update_fails(on_path, monkeypatch, tmp_path) -> None:
    from papaya_agent_runtime.setup import doctor

    monkeypatch.setenv("PPY_HOME", str(tmp_path / ".ppy"))
    client = FakeClient("0.17.0", reinstall=1)
    real_run = papaya._run
    monkeypatch.setattr(
        papaya,
        "_run",
        lambda argv, **kw: (
            client(argv, **kw) if argv[0] in (CLIENT, "uv") else real_run(argv, **kw)
        ),
    )
    data = doctor.collect()
    assert data["papaya_client"]["state"] == "failed"
    assert "could not be updated: run uv tool install --force" in doctor.render_text(data)


# ── the pseudo-terminal ─────────────────────────────────────────────────────

posix_only = pytest.mark.skipif(os.name != "posix", reason="the pty relay is POSIX only")

#: A stand-in client: says whether its stdout is a terminal, then reads one line.
STUB = (
    "import sys\n"
    "print('stdout isatty:', sys.stdout.isatty(), flush=True)\n"
    "line = sys.stdin.readline()\n"
    "print('read:', line.strip(), flush=True)\n"
)


def _terminal():
    """A pty pair standing in for the person's terminal: type into the first, read keys
    from the second."""
    import pty

    return pty.openpty()


def _mode(fd: int) -> list:
    """The terminal's mode, less `PENDIN`: the kernel sets that bit itself when a mode
    change finds typed input still queued, so it says nothing about the restore."""
    import termios

    mode = termios.tcgetattr(fd)
    mode[3] &= ~getattr(termios, "PENDIN", 0)
    return mode


def _type_later(fd: int, data: bytes, delay: float = 0.5) -> threading.Thread:
    typist = threading.Thread(target=lambda: (time.sleep(delay), os.write(fd, data)), daemon=True)
    typist.start()
    return typist


@posix_only
def test_the_client_sees_a_terminal_and_its_output_is_shown_and_kept(tmp_path) -> None:
    stub = tmp_path / "client.py"
    stub.write_text(STUB, encoding="utf-8")
    person, keys = _terminal()
    try:
        before = _mode(keys)
        _type_later(person, b"hello\r")
        echo = io.StringIO()
        code, lines = papaya._on_pty(
            [sys.executable, str(stub)], timeout=30, echo=echo, stdin_fd=keys
        )
        assert code == 0
        assert "stdout isatty: True" in lines
        assert "read: hello" in lines
        shown = echo.getvalue()
        assert "stdout isatty: True" in shown and "read: hello" in shown
        assert _mode(keys) == before
    finally:
        os.close(person)
        os.close(keys)


@posix_only
def test_the_terminal_is_restored_when_the_relay_fails(tmp_path) -> None:
    stub = tmp_path / "client.py"
    stub.write_text("import time\nprint('hello', flush=True)\ntime.sleep(60)\n", encoding="utf-8")

    class Broken(io.StringIO):
        def write(self, text: str) -> int:
            raise RuntimeError("the screen went away")

    person, keys = _terminal()
    try:
        before = _mode(keys)
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="the screen went away"):
            papaya._on_pty([sys.executable, str(stub)], timeout=30, echo=Broken(), stdin_fd=keys)
        assert time.monotonic() - started < 20  # the client was stopped, not waited out
        assert _mode(keys) == before
    finally:
        os.close(person)
        os.close(keys)


@posix_only
def test_the_terminal_is_restored_on_a_timeout_and_what_was_said_rides_the_error(
    tmp_path,
) -> None:
    stub = tmp_path / "client.py"
    stub.write_text(
        "import time\nprint('Open https://app.trypapaya.ai/signin?code=xyz', flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    person, keys = _terminal()
    try:
        before = _mode(keys)
        with pytest.raises(subprocess.TimeoutExpired) as raised:
            papaya._on_pty(
                [sys.executable, str(stub)], timeout=2, echo=io.StringIO(), stdin_fd=keys
            )
        assert "https://app.trypapaya.ai/signin?code=xyz" in raised.value.output
        assert _mode(keys) == before
    finally:
        os.close(person)
        os.close(keys)


@posix_only
def test_ctrl_c_at_the_clients_question_ends_setup(tmp_path) -> None:
    stub = tmp_path / "client.py"
    stub.write_text(STUB, encoding="utf-8")
    person, keys = _terminal()
    try:
        before = _mode(keys)
        _type_later(person, b"\x03")
        with pytest.raises(KeyboardInterrupt):
            papaya._on_pty(
                [sys.executable, str(stub)], timeout=30, echo=io.StringIO(), stdin_fd=keys
            )
        assert _mode(keys) == before
    finally:
        os.close(person)
        os.close(keys)


def test_a_piped_stdin_keeps_the_old_relay(monkeypatch) -> None:
    """No terminal to hand the client, or Windows: the pipe relay, exactly as before."""
    assert papaya._pty_wanted(io.StringIO()) is False
    monkeypatch.setattr(papaya.os, "name", "nt")

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    assert papaya._pty_wanted(Tty()) is False


def test_what_the_pickers_draw_still_reads_as_the_clients_lines() -> None:
    drawn = (
        "\x1b[?25l\x1b[0m? Choose an agent \x1b[2m(Use arrow keys)\x1b[0m\r\n"
        "\x1b[K» Middle Manager\r\n"
        "\x1b[?25h\rConnected as Middle Manager (@mm) in Papaya HQ.\r\n"
    )
    lines = papaya._plain(drawn).splitlines()
    assert papaya._agent_choice(lines) == "asked"
    assert papaya._workspace_named(lines) == "Papaya HQ"
