"""The one step that may rebuild this checkout's environment: `uv sync`, guarded.

`bin/ppy` runs every command with `uv run --no-sync`. Only `ppy serve` (once, before
it starts) and an explicit `ppy env sync` come here, and so does any command on a
checkout whose environment has never been built.

Why it is guarded: on 2026-09-16 a `ppy status` typed in a terminal rebuilt `.venv`
under a running `serve`. The desktop app's uv and the shell's uv chose different
interpreters for the same project, `uv run` "fixed" the environment, and every poll
in `serve` failed with "No such file or directory" until the app was quit — the
interpreter and packages it was importing from had been deleted. A sync is only
safe when nothing is running from the environment, and the thing that runs from it
for hours is the supervisor, which holds ``<PPY_HOME>/run/supervisor.lock`` with
``flock`` for its whole life (`supervisor/server.py`). So this takes the same lock:
held by somebody else means refuse and name them; free means hold it for the length
of the sync, so a `serve` cannot come up on a half-built environment either.

Run by `bin/ppy` under whatever interpreter it can find — possibly an old system
`python3` — so this module stays standard-library only and 3.9-compatible, like
`capabilities.py`.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
import sys

#: Exit status when another process holds the lock (`EX_TEMPFAIL`).
REFUSED = 75


def lock_path() -> str:
    """The supervisor's owner lock for this ``PPY_HOME`` (see `owner_lock_path`)."""
    home = os.environ.get("PPY_HOME") or os.path.join(os.getcwd(), ".ppy")
    return os.path.join(os.path.realpath(home), "run", "supervisor.lock")


def _recorded_pid(fd: int) -> str:
    try:
        return os.pread(fd, 32, 0).decode("utf-8", "replace").strip()
    except OSError:
        return ""


def sync_command(root: str, python: str | None) -> list[str]:
    argv = ["uv", "sync", "--frozen", "--project", root]
    if python:
        argv += ["--python", python]
    return argv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ppy env sync")
    parser.add_argument("--project", required=True)
    parser.add_argument("--python", default=None)
    args = parser.parse_args(argv)

    path = lock_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            pid = _recorded_pid(fd)
            print(
                "ppy: refusing to sync the environment: "
                + (f"pid {pid}" if pid else "another process")
                + f" holds {path}, so a `ppy serve` or supervisor is running from it. "
                "Stop it first; its next start syncs.",
                file=sys.stderr,
            )
            return REFUSED
        # Ours now; the pid is advisory, for whoever is refused while we sync.
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        return subprocess.call(sync_command(args.project, args.python))
    finally:
        os.close(fd)


if __name__ == "__main__":
    sys.exit(main())
