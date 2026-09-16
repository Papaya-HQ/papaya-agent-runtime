"""`bin/ppy` builds the environment once, never under a running `serve`.

On 2026-09-16 a `ppy status` typed in a terminal rebuilt `.venv` under the desktop
app's `ppy serve`: the two invokers' uv picked different interpreters, `uv run`
re-synced, and `serve` lost the files it was running from. These drive the real
launcher with a fake `uv` on `PATH` that records its argv, so what the launcher asks
uv to do is the thing under test.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from conftest import scale
from papaya_agent_runtime.setup import doctor
from papaya_agent_runtime.supervisor.server import SupervisorServer

ROOT = Path(__file__).resolve().parents[1]


def _launcher(tmp_path: Path) -> tuple[dict, Path, Path]:
    """An environment with a fake `uv`, a scratch project environment and instance."""
    log = tmp_path / "uv-argv.log"
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    uv = fake_bin / "uv"
    uv.write_text(
        f'#!/bin/sh\nfor a in "$@"; do printf \'%s\\t\' "$a"; done >>"{log}"\necho >>"{log}"\n'
    )
    uv.chmod(0o755)

    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(sys.executable)
    (venv / "pyvenv.cfg").write_text("version_info = 3.13.0\n")

    env = {
        **os.environ,
        "PATH": os.pathsep.join([str(fake_bin), os.environ.get("PATH", "")]),
        "UV_PROJECT_ENVIRONMENT": str(venv),
        "PPY_HOME": str(tmp_path / ".ppy"),
    }
    return env, log, venv


def _ppy(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(ROOT / "bin" / "ppy"), *args],
        capture_output=True,
        text=True,
        timeout=scale(60),
        env=env,
        check=False,
    )


def _calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [line.rstrip("\t").split("\t") for line in log.read_text().splitlines()]


def test_serve_refuses_to_sync_while_the_supervisor_lock_is_held(tmp_path, monkeypatch) -> None:
    env, log, _ = _launcher(tmp_path)
    monkeypatch.setenv("PPY_HOME", env["PPY_HOME"])
    holder = SupervisorServer.__new__(SupervisorServer)
    holder._acquire_owner_lock()  # exactly what a running supervisor holds
    try:
        proc = _ppy(env, "serve", "--help")
    finally:
        holder._release_owner_lock()

    assert proc.returncode != 0
    assert "refusing to sync" in proc.stderr
    assert f"pid {os.getpid()}" in proc.stderr
    assert _calls(log) == [], "the launcher reached uv while the lock was held"


def test_a_command_with_an_environment_present_never_syncs(tmp_path) -> None:
    env, log, _ = _launcher(tmp_path)

    proc = _ppy(env, "capabilities", "--json")

    assert proc.returncode == 0, proc.stderr
    calls = _calls(log)
    assert len(calls) == 1, calls
    assert calls[0][:2] == ["run", "--no-sync"]
    assert "sync" not in calls[0][2:]


def test_the_launcher_asks_uv_for_the_series_python_version_pins(tmp_path) -> None:
    env, log, _ = _launcher(tmp_path)
    pinned = (ROOT / ".python-version").read_text().strip()

    proc = _ppy(env, "serve", "--help")

    assert proc.returncode == 0, proc.stderr
    calls = _calls(log)
    # `serve` syncs once, then runs without syncing; both ask for the pinned series.
    assert [c[:2] for c in calls] == [["sync", "--frozen"], ["run", "--no-sync"]]
    for call in calls:
        assert call[call.index("--python") + 1] == pinned


def test_doctor_warns_when_the_environment_runs_another_series(tmp_path) -> None:
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("home = /x/bin\nversion_info = 3.14.0\n")
    pin = tmp_path / ".python-version"
    pin.write_text("3.13\n")

    status = doctor.venv_interpreter(venv, pin)

    assert status["version"] == "3.14.0"
    assert "3.14.0" in status["warning"] and "3.13" in status["warning"]
    assert "WARNING" in doctor._venv_line(status)

    (venv / "pyvenv.cfg").write_text("home = /x/bin\nversion_info = 3.13.7\n")
    assert doctor.venv_interpreter(venv, pin)["warning"] is None
