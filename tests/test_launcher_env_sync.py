"""`bin/ppy` builds the environment once, never under a running `serve`, and never half.

On 2026-09-16 a `ppy status` typed in a terminal rebuilt `.venv` under the desktop
app's `ppy serve`: the two invokers' uv picked different interpreters, `uv run`
re-synced, and `serve` lost the files it was running from. Later the same day the
guard that fixed that refused every restart while the previous build's supervisor
held the lock, and left the environment without the Papaya client. These drive the
real launcher with a fake `uv` on `PATH` that records its argv, builds a stand-in
environment when asked to sync, and runs Python when asked to run, so what the
launcher asks uv to do — and in which order — is the thing under test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from conftest import scale, wait_until
from papaya_agent_runtime import envsync, takeover
from papaya_agent_runtime.setup import doctor
from papaya_agent_runtime.supervisor.server import SupervisorServer

ROOT = Path(__file__).resolve().parents[1]

#: A stand-in `uv`: logs its argv, builds an environment for `sync`, runs `python` for `run`.
FAKE_UV = """#!/bin/sh
for a in "$@"; do printf '%s\\t' "$a"; done >>"{log}"
echo >>"{log}"
case "$1" in
    sync)
        if [ -n "${{FAKE_UV_SYNC:-}}" ]; then
            mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"
            eval "$FAKE_UV_SYNC"
        fi
        mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"
        printf '#!/bin/sh\\nexec "{python}" "$@"\\n' >"$UV_PROJECT_ENVIRONMENT/bin/python"
        chmod +x "$UV_PROJECT_ENVIRONMENT/bin/python"
        ;;
    run)
        while [ "$#" -gt 0 ] && [ "$1" != "python" ]; do shift; done
        [ "$#" -gt 0 ] || exit 0
        shift
        exec "{python}" "$@"
        ;;
esac
"""


def _interpreter(path: Path, *, imports_client: bool = True) -> None:
    """An environment interpreter: the test's own, or one whose site-packages are gone."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = "" if imports_client else " -S"
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}"{flags} "$@"\n')
    path.chmod(0o755)


def _launcher(tmp_path: Path) -> tuple[dict, Path, Path]:
    """An environment with a fake `uv`, a scratch project environment and instance."""
    log = tmp_path / "uv-argv.log"
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    uv = fake_bin / "uv"
    uv.write_text(FAKE_UV.format(log=log, python=sys.executable))
    uv.chmod(0o755)

    venv = tmp_path / "venv"
    _interpreter(venv / "bin" / "python")
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
        timeout=scale(90),
        env=env,
        check=False,
    )


def _calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [line.rstrip("\t").split("\t") for line in log.read_text().splitlines()]


def _verbs(log: Path) -> list[list[str]]:
    return [call[:2] for call in _calls(log)]


# A supervisor in a process of its own, the way the app's `serve` is one: it serves
# until it is asked to shut down, then stops its workers and exits.
_HOLDER = """
import json, os, sys
from papaya_agent_runtime.supervisor.server import SupervisorServer
server = SupervisorServer(role="serve")
server._bind()
if sys.argv[1] != "-":
    record = os.path.join(os.environ["PPY_HOME"], "run", "supervisor.json")
    data = json.load(open(record))
    data["build_id"] = sys.argv[1]
    json.dump(data, open(record, "w"))
print("owner " + str(os.getpid()), flush=True)
server._serve_loop()
server.shutdown()
"""


@pytest.fixture
def holder(tmp_path):
    started: list[subprocess.Popen] = []

    def start(env: dict, build_id: str = "-") -> subprocess.Popen:
        child = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, build_id],
            env={**env, "PYTHONPATH": str(ROOT / "src")},
            stdout=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
        )
        started.append(child)
        assert child.stdout is not None
        assert child.stdout.readline().startswith("owner "), "the holder did not start"
        # Reaped as soon as it exits, as the app reaps its `serve`: a zombie still
        # answers `kill(pid, 0)` and would look like a holder that will not go.
        threading.Thread(target=child.wait, daemon=True).start()
        return child

    yield start
    for child in started:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def test_serve_retires_a_supervisor_of_another_build_before_it_syncs(tmp_path, holder) -> None:
    env, log, _ = _launcher(tmp_path)
    old = holder(env, build_id="0.0.0+0123456789ab")

    proc = _ppy(env, "serve", "--help")

    assert proc.returncode == 0, proc.stderr
    assert old.wait(timeout=scale(30)) == 0, "the old supervisor was not retired"
    lines = [line for line in proc.stderr.splitlines() if "retired supervisor" in line]
    assert len(lines) == 1, proc.stderr
    assert f"pid {old.pid}" in lines[0] and "0.0.0+0123456789ab" in lines[0]
    assert "asked it to shut down" in lines[0]
    # Retire first, then sync, then run: the sync never meets a supervisor it did not keep.
    assert _verbs(log) == [["sync", "--frozen"], ["run", "--no-sync"]]


def test_serve_adopts_a_supervisor_of_this_build_and_rebuilds_nothing(tmp_path, holder) -> None:
    env, log, _ = _launcher(tmp_path)
    live = holder(env)  # the record it writes is this checkout's own build

    proc = _ppy(env, "serve", "--help")

    assert proc.returncode == 0, proc.stderr
    assert live.poll() is None, "a supervisor of this build was stopped"
    assert _verbs(log) == [["run", "--no-sync"]]


def test_an_explicit_env_sync_still_refuses_while_a_supervisor_holds_the_lock(
    tmp_path, monkeypatch
) -> None:
    env, log, _ = _launcher(tmp_path)
    monkeypatch.setenv("PPY_HOME", env["PPY_HOME"])
    holding = SupervisorServer.__new__(SupervisorServer)
    holding._acquire_owner_lock()  # exactly what a running supervisor holds
    try:
        proc = _ppy(env, "env", "sync")
    finally:
        holding._release_owner_lock()

    assert proc.returncode == envsync.REFUSED
    assert "refusing to sync" in proc.stderr
    assert f"pid {os.getpid()}" in proc.stderr
    assert _calls(log) == [], "the launcher reached uv while the lock was held"


@pytest.mark.parametrize("environment", ["imports", "missing"])
def test_supervisor_stop_works_while_the_sync_would_be_refused(
    tmp_path, monkeypatch, environment
) -> None:
    env, log, venv = _launcher(tmp_path)
    if environment == "missing":
        _interpreter(venv / "bin" / "python", imports_client=False)
    monkeypatch.setenv("PPY_HOME", env["PPY_HOME"])
    server = SupervisorServer()
    server.start_background()
    try:
        refused = _ppy(env, "env", "sync")
        assert refused.returncode == envsync.REFUSED  # the sync would be refused

        proc = _ppy(env, "supervisor", "stop")

        assert proc.returncode == 0, proc.stderr
        assert "supervisor shutdown requested" in proc.stdout
        wait_until(lambda: server._stop.is_set(), 10, what="the supervisor to shut down")
    finally:
        server.stop()
    assert all(call[0] != "sync" for call in _calls(log)), _calls(log)


def test_an_interrupted_sync_leaves_the_previous_environment_importable(tmp_path) -> None:
    env, log, venv = _launcher(tmp_path)
    before = (venv / "bin" / "python").read_text()
    # The sync is killed part-way: uv has written half an environment and the
    # process driving it dies without running another line.
    env["FAKE_UV_SYNC"] = 'touch "$UV_PROJECT_ENVIRONMENT/half"; kill -9 $PPID; sleep 1'

    proc = _ppy(env, "env", "sync")

    assert proc.returncode != 0
    assert venv.is_dir() and not venv.is_symlink()
    assert (venv / "bin" / "python").read_text() == before
    assert envsync.importable(str(venv)), "the previous environment no longer imports"

    # The next sync builds a whole one beside it, swaps it in, and clears the half.
    del env["FAKE_UV_SYNC"]
    again = _ppy(env, "env", "sync")
    assert again.returncode == 0, again.stderr
    assert venv.is_symlink() and envsync.importable(str(venv))
    assert not list(tmp_path.glob("venv.env-*/half"))


def test_a_sync_that_does_not_produce_an_importable_environment_changes_nothing(
    tmp_path,
) -> None:
    env, _log, venv = _launcher(tmp_path)
    env["FAKE_UV_SYNC"] = (
        'printf \'#!/bin/sh\\nexec "%s" -S "$@"\\n\' "' + sys.executable + '" '
        '>"$UV_PROJECT_ENVIRONMENT/bin/python"; chmod +x "$UV_PROJECT_ENVIRONMENT/bin/python"; '
        "exit 0"
    )

    proc = _ppy(env, "env", "sync")

    assert proc.returncode == envsync.FAILED
    assert "previous environment is left as it was" in proc.stderr
    assert not venv.is_symlink() and envsync.importable(str(venv))
    assert list(tmp_path.glob("venv.env-*")) == []


def test_serve_records_a_sync_it_could_not_do_for_the_blockers_ledger(tmp_path) -> None:
    env, _log, _venv = _launcher(tmp_path)
    env["FAKE_UV_SYNC"] = "exit 2"

    proc = _ppy(env, "serve", "--help")

    assert proc.returncode == 2
    said = [line for line in proc.stderr.splitlines() if line.strip()]
    assert len(said) == 1, proc.stderr
    record = takeover.start_failure(str(Path(env["PPY_HOME"]).resolve()))
    assert record is not None and "`uv sync` exited 2" in record["line"]
    ledger = json.loads((Path(env["PPY_HOME"]) / "blockers.json").read_text())
    (entry,) = ledger["open"].values()
    assert entry["code"] == takeover.START_FAILURE_CODE


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

    # Built from this lockfile and importing: the next start rebuilds nothing.
    again = _ppy(env, "serve", "--help")
    assert again.returncode == 0, again.stderr
    assert [c[:2] for c in _calls(log)[2:]] == [["run", "--no-sync"]]


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
