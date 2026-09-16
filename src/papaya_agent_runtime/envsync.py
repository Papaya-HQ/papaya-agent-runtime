"""The one step that may rebuild this checkout's environment: `uv sync`, guarded.

`bin/ppy` runs every command with `uv run --no-sync`. Only `ppy serve` (once, before
it starts, and only when the environment is out of date or broken) and an explicit
`ppy env sync` come here, and so does any command on a checkout whose environment
has never been built.

Why it is guarded: on 2026-09-16 a `ppy status` typed in a terminal rebuilt `.venv`
under a running `serve`. The desktop app's uv and the shell's uv chose different
interpreters for the same project, `uv run` "fixed" the environment, and every poll
in `serve` failed with "No such file or directory" until the app was quit — the
interpreter and packages it was importing from had been deleted. A sync is only
safe when nothing is running from the environment, and the thing that runs from it
for hours is the supervisor, which holds ``<PPY_HOME>/run/supervisor.lock`` with
``flock`` for its whole life (`supervisor/server.py`). So this takes the same lock:
held by somebody else means refuse and name them; free means hold it for the length
of the sync, so a `serve` cannot come up on a half-built environment either. A lock
file naming a pid that is no longer running is a stale holder: the lock is taken
and one line says so.

Why it is atomic: later the same day a refused sync was not the end of it. The
environment had been left without the Papaya client, so nothing imported until a
person rebuilt it by hand. So a sync never touches the environment in use. It
builds a fresh directory beside it (``<env>.env-<stamp>``), checks the client
imports from it, and only then points the environment path at it with one
``rename`` of a symlink. A refused, failed or interrupted sync leaves the previous
environment exactly as it was. The first sync of a checkout whose environment is a
plain directory moves that directory aside and puts the symlink in its place — two
renames, once.

Run by `bin/ppy` under whatever interpreter it can find — possibly an old system
`python3` — so this module stays standard-library only and 3.9-compatible, like
`capabilities.py`.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import glob
import hashlib
import os
import shutil
import subprocess
import sys
import time

#: Exit status when another process holds the lock (`EX_TEMPFAIL`).
REFUSED = 75

#: Exit status when the sync ran and did not produce an environment that imports.
FAILED = 1

#: The file in a built environment recording what it was built from.
STAMP = ".ppy-env-stamp"

#: What must import from an environment for it to be this runtime's.
IMPORT_CHECK = "import papaya_agent_client"


def lock_path() -> str:
    """The supervisor's owner lock for this ``PPY_HOME`` (see `owner_lock_path`)."""
    home = os.environ.get("PPY_HOME") or os.path.join(os.getcwd(), ".ppy")
    return os.path.join(os.path.realpath(home), "run", "supervisor.lock")


def environment_path(root: str) -> str:
    """Where `uv run` finds this checkout's environment: `UV_PROJECT_ENVIRONMENT` or `.venv`."""
    configured = os.environ.get("UV_PROJECT_ENVIRONMENT")
    if configured:
        return os.path.abspath(configured)
    return os.path.join(os.path.abspath(root), ".venv")


def _recorded_pid(fd: int) -> str:
    try:
        return os.pread(fd, 32, 0).decode("utf-8", "replace").strip()
    except OSError:
        return ""


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def sync_command(root: str, python: str | None) -> list[str]:
    argv = ["uv", "sync", "--frozen", "--project", root]
    if python:
        argv += ["--python", python]
    return argv


def wanted_stamp(root: str, python: str | None) -> str:
    """What an up-to-date environment was built from: the lockfile, the project, the series."""
    digest = hashlib.sha256()
    for name in ("uv.lock", "pyproject.toml"):
        try:
            with open(os.path.join(root, name), "rb") as fh:
                digest.update(fh.read())
        except OSError:
            digest.update(b"-")
    digest.update((python or "").encode("utf-8"))
    return digest.hexdigest()


def built_stamp(env: str) -> str:
    try:
        with open(os.path.join(env, STAMP), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def importable(env: str, *, timeout: float = 60.0) -> bool:
    """Does the runtime's dependency import from this environment's own interpreter?"""
    python = os.path.join(env, "bin", "python")
    if not os.path.exists(python):
        return False
    child = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "VIRTUAL_ENV")}
    try:
        return (
            subprocess.call(
                [python, "-c", IMPORT_CHECK],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=child,
                timeout=timeout,
            )
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def up_to_date(root: str, python: str | None) -> bool:
    env = environment_path(root)
    return built_stamp(env) == wanted_stamp(root, python) and importable(env)


def _fresh_path(env: str) -> str:
    return f"{env}.env-{time.strftime('%Y%m%d%H%M%S')}-{os.getpid()}"


def swap_in(env: str, fresh: str) -> str | None:
    """Point ``env`` at ``fresh``. Returns what it pointed at before, if anything."""
    previous: str | None = None
    target = os.path.basename(fresh)
    if os.path.islink(env):
        previous = os.path.join(os.path.dirname(env), os.readlink(env))
        temp = f"{env}.swap-{os.getpid()}"
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
        os.symlink(target, temp)
        os.replace(temp, env)  # one rename: there is never a moment with no environment
        return previous
    if os.path.isdir(env):
        # A plain directory cannot be replaced by a rename; move it aside first.
        previous = f"{env}.env-{time.strftime('%Y%m%d%H%M%S')}-{os.getpid()}-previous"
        os.rename(env, previous)
    os.symlink(target, env)
    return previous


def prune(env: str, keep: set[str]) -> None:
    """Remove built environments nothing points at any more, keeping ``keep``."""
    kept = {os.path.realpath(path) for path in keep if path}
    for path in glob.glob(f"{glob.escape(env)}.env-*"):
        if os.path.realpath(path) in kept:
            continue
        shutil.rmtree(path, ignore_errors=True)


def build(root: str, python: str | None, *, stderr=None) -> int:
    """Build a fresh environment and swap it in; the one in use is untouched on any failure."""
    stderr = stderr or sys.stderr
    env = environment_path(root)
    fresh = _fresh_path(env)
    child = dict(os.environ, UV_PROJECT_ENVIRONMENT=fresh)
    try:
        code = subprocess.call(sync_command(root, python), env=child)
    except OSError as exc:
        shutil.rmtree(fresh, ignore_errors=True)
        print(f"ppy: could not rebuild the environment: {exc}", file=stderr)
        return FAILED
    if code != 0 or not importable(fresh):
        shutil.rmtree(fresh, ignore_errors=True)
        print(
            "ppy: could not rebuild the environment: "
            + (f"`uv sync` exited {code}" if code else "the Papaya client does not import from it")
            + "; the previous environment is left as it was.",
            file=stderr,
        )
        return code or FAILED
    with open(os.path.join(fresh, STAMP), "w", encoding="utf-8") as fh:
        fh.write(wanted_stamp(root, python) + "\n")
    previous = swap_in(env, fresh)
    prune(env, {fresh, previous or ""})
    return 0


class _Said:
    """stderr, remembering the last line said: the sentence a failed start is recorded as."""

    def __init__(self, stream) -> None:
        self.stream = stream
        self.text = ""

    def write(self, text: str) -> int:
        self.text += text
        return self.stream.write(text)

    def flush(self) -> None:
        self.stream.flush()

    @property
    def last_line(self) -> str:
        lines = [line for line in self.text.splitlines() if line.strip()]
        return lines[-1] if lines else ""


def main(argv: list[str] | None = None, *, stderr=None) -> int:
    parser = argparse.ArgumentParser(prog="ppy env sync")
    parser.add_argument("--project", required=True)
    parser.add_argument("--python", default=None)
    parser.add_argument(
        "--if-needed",
        action="store_true",
        help="do nothing when the environment was built from this lockfile and imports",
    )
    parser.add_argument(
        "--record-failure",
        action="store_true",
        help="a `ppy serve` start: record a failure for the blockers ledger",
    )
    args = parser.parse_args(argv)
    root = os.path.abspath(args.project)
    said = _Said(stderr or sys.stderr)
    code = _sync(root, args, said)
    if code and args.record_failure:
        from papaya_agent_runtime import takeover

        line = said.last_line.split("ppy: ", 1)[-1] or f"the environment sync exited {code}"
        with contextlib.suppress(OSError):
            takeover.record_start_failure(takeover.home_dir(), line, ["ppy env sync"])
    return code


def _sync(root: str, args: argparse.Namespace, stderr) -> int:
    if args.if_needed and up_to_date(root, args.python):
        return 0

    path = lock_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        recorded = _recorded_pid(fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            pid = int(recorded) if recorded.isdigit() else None
            holder = f"pid {recorded}" if recorded else "another process"
            if pid and not pid_alive(pid):
                holder = f"a process other than pid {recorded} (which is no longer running)"
            print(
                "ppy: refusing to sync the environment: "
                + holder
                + f" holds {path}, so a `ppy serve` or supervisor is running from it. "
                "Stop it first (`ppy supervisor stop`); its next start syncs.",
                file=stderr,
            )
            return REFUSED
        try:
            if recorded.isdigit() and int(recorded) != os.getpid() and not pid_alive(int(recorded)):
                print(
                    f"ppy: took the supervisor lock from pid {recorded}, "
                    "which is no longer running",
                    file=stderr,
                )
            # Ours now; the pid is advisory, for whoever is refused while we sync.
            os.ftruncate(fd, 0)
            os.pwrite(fd, str(os.getpid()).encode(), 0)
            return build(root, args.python, stderr=stderr)
        finally:
            # Leave no pid behind: a pid in the file after its process is gone is
            # how the next start recognises a crashed holder.
            with contextlib.suppress(OSError):
                os.ftruncate(fd, 0)
    finally:
        os.close(fd)


if __name__ == "__main__":
    sys.exit(main())
