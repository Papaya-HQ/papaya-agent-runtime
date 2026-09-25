"""The Papaya connection: who this runtime is, and what it can write back.

This runtime has no fixed persona. It *is* whichever Papaya agent this machine is
connected as — the name, handle, workspace, persona, rules and memories all come
from Papaya, not from a document in this repository. `papaya-agent connect` is what
establishes that: it signs the machine in, pins it to one agent, and installs the
Claude Code plugin that carries the `papaya` MCP server, the `papaya-connect` skill
and the rule-enforcing hooks.

The split this module keeps:

- **Mechanics live here.** Is the client installed? Is this machine connected, and
  as whom? If not, run the connect flow. Which Papaya work item does a local task
  belong to?
- **Everything else lives in the harness.** Reading work, posting comments,
  proposing memories and searching the workspace are MCP tool calls the agent makes
  directly; shelling out to re-implement them here would be slower and lossier.
  That includes which *tracker* a workspace uses — Papaya work items, Linear, Notion
  or anything else. This module knows about the connection, never about the work;
  `papaya_agent_runtime.tracker` records a task's tracked record without caring where
  it lives.

The connection is a *preference*, never a prerequisite. A runtime with no Papaya
reachable still registers repositories, dispatches workers, reviews diffs and opens
pull requests — it just cannot see the workspace. Every function here says which of
those two worlds it is in rather than raising.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

#: The command `papaya-agent connect` installs onto the PATH.
CLI = "papaya-agent"
#: How to reach the client when it is not installed yet. The npm package is a shim
#: that finds or downloads `uv`, runs the Python client, and after a successful
#: `connect` installs it for good (`uv tool install`), so `papaya-agent` is on the
#: PATH for the Claude Code plugin's hooks and the `papaya` MCP server.
BOOTSTRAP = ("npx", "--yes", "papaya-agent")
#: The Python package the shim runs. Reached directly through `uv` when this machine
#: has no Node: the runtime always has `uv`, and the shim is only a wrapper around it.
CLIENT_PACKAGE = "papaya-agent-client"
#: uv's own output for every uv call made for the client: no resolve, download or
#: "Installed N packages" lines. Errors still print, and the client's own output is
#: untouched: `--quiet` belongs to uv, not to the tool it runs.
UV_QUIET = "--quiet"
UV_BOOTSTRAP = ("uv", "tool", "run", UV_QUIET, "--from", CLIENT_PACKAGE, CLI)
#: What the shim does after a successful connect, done by hand on the `uv` path;
#: :func:`uv_install_argv` adds the version this runtime locks.
UV_INSTALL = ("uv", "tool", "install", UV_QUIET, CLIENT_PACKAGE)
#: uv's progress bars, off for the npm shim's uv too, which `--quiet` cannot reach.
UV_NO_PROGRESS = "UV_NO_PROGRESS"
#: The client's answer when a choice is needed and nobody is at a terminal to make it:
#: `Multiple Papaya agents found. Re-run with `--agent <agent>`. Available: a; b`.
_CHOICE = re.compile(
    r"Multiple Papaya (?P<kind>workspace|agent)s found\. Re-run with `(?P<flag>--\w+) "
    r"<\w+>`\. Available: (?P<choices>.+)$"
)
_LINK = re.compile(r"https?://\S+")
#: Overridable for tests, and the last word when set.
HOME_ENV = "PPY_PAPAYA_HOME"
#: The Papaya client's own override. The desktop app sets this for the process it
#: launches, which is why a connection made there is invisible to a shell that did
#: not inherit it — hence the discovery below.
CLIENT_HOME_ENV = "PAPAYA_AGENT_HOME"
#: How long a connect flow may sit waiting for the person to click Approve.
CONNECT_TIMEOUT = 300
#: Short probes (reading identity, reading context) should never hang a preflight.
PROBE_TIMEOUT = 30


def _desktop_home() -> Path | None:
    """Where the Papaya desktop app's bundled client keeps its connection.

    The app runs the client as a child process with `PAPAYA_AGENT_HOME` pointed
    here, so the connection is real but invisible to any shell that did not inherit
    that variable — which is every shell the user opens themselves. Looking here is
    what makes "connect from the desktop app" and "connect from the CLI" the same
    thing to this runtime.
    """
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        base = Path(appdata)
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "Papaya" / "agent-host" / "client"


def candidate_homes() -> list[Path]:
    """Every place a Papaya connection could have been established, best first.

    An explicit `PPY_PAPAYA_HOME` is the whole list: it exists so a test can say
    precisely what the world looks like.
    """
    override = os.environ.get(HOME_ENV)
    if override:
        return [Path(override)]
    homes: list[Path] = []
    client_override = os.environ.get(CLIENT_HOME_ENV)
    if client_override:
        homes.append(Path(client_override))
    homes.append(Path.home() / ".papaya-agent")
    desktop = _desktop_home()
    if desktop is not None:
        homes.append(desktop)
    seen: list[Path] = []
    for home in homes:
        if home not in seen:
            seen.append(home)
    return seen


def client_home() -> Path:
    """The home this machine's connection actually lives in.

    The one carrying a pinned agent, or the first candidate when none does — so an
    unconnected machine still reports a sensible path to look at.
    """
    found = _best()
    if found is not None:
        return found[1]
    return candidate_homes()[0]


def config_path() -> Path:
    return client_home() / "config.json"


@dataclass(frozen=True)
class Identity:
    """The Papaya agent this machine is connected as.

    Read from the client's own config rather than the network, so a preflight can
    answer "who am I" instantly and offline. `handle` is the address other people
    and agents use; `name` is only a display label, so anything user-visible should
    lead with the handle.
    """

    agent_id: str = ""
    name: str = ""
    handle: str = ""
    role_label: str = ""
    workspace_id: str = ""
    connection_id: str = ""
    harness: str = ""

    @property
    def addressed(self) -> str:
        """How to refer to this agent in copy: the handle, falling back to the name."""
        if self.handle:
            return f"@{self.handle.lstrip('@')}"
        return self.name or "an unnamed Papaya agent"


#: What `agent:` says in a turn's facts: one person's agent, or one a workspace shares.
AGENT_PERSONAL = "personal"
AGENT_SHARED = "shared"
#: What `memory:` says: where a turn keeps a durable fact.
MEMORY_PAPAYA = "papaya"
MEMORY_REPO_NOTES_ONLY = "repo-notes-only"


@dataclass(frozen=True)
class AgentKind:
    """Whether the connected agent is shared, and so where its turns may keep memory.

    Papaya refuses a machine-extracted agent-scoped memory on a shared agent, because
    that memory would be visible to the whole workspace (backend `polyweave_memory`).
    Only a personal agent's turns may call `propose_memory`; a shared agent's durable
    facts go to the repository's memory notes instead.
    """

    agent: str

    @property
    def memory(self) -> str:
        return MEMORY_PAPAYA if self.agent == AGENT_PERSONAL else MEMORY_REPO_NOTES_ONLY

    def facts(self) -> dict[str, str]:
        return {"agent": self.agent, "memory": self.memory}


def agent_kind_of(record: object) -> AgentKind | None:
    """The kind of agent Papaya's agent record describes, or ``None`` when it does not say.

    ``ownership_scope`` is ``personal`` or ``workspace``; a workspace agent is shared.
    """
    scope = record.get("ownership_scope") if isinstance(record, dict) else None
    if scope == "personal":
        return AgentKind(AGENT_PERSONAL)
    if scope == "workspace":
        return AgentKind(AGENT_SHARED)
    return None


def agent_kinds_path() -> Path:
    """Where the kind of each agent this runtime has served as is kept: `.ppy/agent-kinds.json`."""
    from papaya_agent_runtime.paths import ppy_home

    return ppy_home() / "agent-kinds.json"


def _agent_kinds() -> dict:
    try:
        data = json.loads(agent_kinds_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def remember_agent_kind(agent_id: str, kind: AgentKind) -> None:
    """Keep what Papaya said this agent is, for readiness and the next start. Never raises."""
    if not agent_id:
        return
    known = _agent_kinds()
    if (known.get(agent_id) or {}).get("agent") == kind.agent:
        return
    known[agent_id] = {"agent": kind.agent}
    path = agent_kinds_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(known, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return


def known_agent_kind(agent_id: str | None) -> AgentKind | None:
    """What this runtime last learned the agent ``agent_id`` is, or ``None``."""
    entry = _agent_kinds().get(agent_id or "")
    agent = entry.get("agent") if isinstance(entry, dict) else None
    return AgentKind(agent) if agent in (AGENT_PERSONAL, AGENT_SHARED) else None


def installed() -> str | None:
    """The person's own `papaya-agent` on the PATH, or None when they have none yet.

    The runtime's virtualenv bundles the client (``ppy serve`` embeds it), and ``uv
    run`` puts that environment's ``bin`` first on the PATH, so a plain lookup always
    found the bundled copy: every machine looked installed, ``absent`` never happened,
    and nothing ever set the client up for real. The bundled copy is also invisible to
    the Claude Code plugin's hooks and the ``papaya`` MCP server, which run outside this
    environment. So that directory is skipped.
    """
    own = {Path(sys.prefix).resolve() / "bin", Path(sys.executable).resolve().parent}
    search = os.pathsep.join(
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry and Path(entry).resolve() not in own
    )
    return shutil.which(CLI, path=search)


def _read_config(home: Path) -> dict:
    path = home / "config.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _identity_in(config: dict) -> tuple[Identity, str] | None:
    """The pinned agent in one config, with when it was pinned.

    `papaya-agent connect` writes the pinned agent under `connect.agent_id` and the
    full entry under `agents[<id>]`. A config carrying agents but no `connect` block
    is an older client, so the single agent entry is used when there is exactly one
    — guessing between several would act in the workspace as the wrong agent.
    """
    agents = config.get("agents")
    if not isinstance(agents, dict) or not agents:
        return None
    connect = config.get("connect") if isinstance(config.get("connect"), dict) else {}
    entry: dict | None = agents.get(str(connect.get("agent_id") or "")) if connect else None
    if entry is None and len(agents) == 1:
        entry = next(iter(agents.values()))
    if not isinstance(entry, dict):
        return None
    found = Identity(
        agent_id=str(entry.get("agent_id") or ""),
        name=str(entry.get("agent_name") or ""),
        handle=str(entry.get("agent_handle") or ""),
        role_label=str(entry.get("agent_role_label") or ""),
        workspace_id=str(entry.get("workspace_id") or ""),
        connection_id=str(entry.get("connection_id") or ""),
        harness=str(connect.get("harness") or ""),
    )
    return found, str(connect.get("updated_at") or "")


def _pinned() -> tuple[str, Identity, Path] | None:
    """The connection to act as, with when it was pinned and the home it lives in.

    A machine can hold a CLI connection and a desktop-app connection at once. The
    most recently pinned one wins, because that is the one the person last chose;
    an ISO-8601 timestamp sorts correctly as a string, and a config too old to carry
    one loses to any that does.
    """
    found: list[tuple[str, Identity, Path]] = []
    for home in candidate_homes():
        result = _identity_in(_read_config(home))
        if result is not None:
            who, updated_at = result
            found.append((updated_at, who, home))
    if not found:
        return None
    found.sort(key=lambda item: item[0], reverse=True)
    return found[0]


def _best() -> tuple[Identity, Path] | None:
    """The connection to act as, across every place one could have been made."""
    found = _pinned()
    return (found[1], found[2]) if found is not None else None


def _connection_mark() -> tuple[str, str] | None:
    """What only a completed connect rewrites: the pin's stamp and the token's.

    `papaya-agent connect` writes both when it records a connection, and nothing
    else does — signing in rewrites the config too, so the file changing proves
    nothing. ``None`` when nothing is pinned.
    """
    found = _pinned()
    if found is None:
        return None
    updated_at, who, home = found
    agents = _read_config(home).get("agents")
    entry = agents.get(who.agent_id) if isinstance(agents, dict) else None
    token_at = str(entry.get("client_token_updated_at") or "") if isinstance(entry, dict) else ""
    return updated_at, token_at


def identity() -> Identity | None:
    """The agent this machine is pinned to, or None when nothing is connected."""
    found = _best()
    return found[0] if found is not None else None


def stored_working_directory() -> str | None:
    """The working folder the connection was made with, or None when it names none.

    `papaya-agent connect --working-directory` (the desktop app's path) stores it on
    the pinned agent's entry; a device-code connect never does.
    """
    found = _best()
    if found is None:
        return None
    config = _read_config(found[1])
    agents = config.get("agents") if isinstance(config.get("agents"), dict) else {}
    entry = agents.get(found[0].agent_id) if found[0].agent_id else None
    if not isinstance(entry, dict):
        return None
    return str(entry.get("working_directory") or "").strip() or None


def signed_in() -> bool:
    """Is any candidate home authenticated, even with no agent pinned yet?"""
    for home in candidate_homes():
        session = _read_config(home).get("session")
        if isinstance(session, dict) and session.get("refresh_token"):
            return True
    return False


def status() -> dict:
    """One dictionary describing the connection, for `ppy doctor` and preflight.

    `state` is the only field a caller should branch on:

    - ``connected`` — pinned to an agent; the workspace is available.
    - ``signed_in`` — authenticated but no agent pinned; connect finishes it.
    - ``installed`` — the client is present but this machine is not signed in.
    - ``absent`` — no client of the person's own; `ppy papaya connect` installs one
      (:func:`installer` says how) and connects it.
    """
    path = installed()
    who = identity()
    if who is not None:
        state = "connected"
    elif signed_in():
        state = "signed_in"
    elif path:
        state = "installed"
    else:
        state = "absent"
    return {
        "state": state,
        "cli": path,
        "config": str(config_path()),
        "searched": [str(home) for home in candidate_homes()],
        "identity": asdict(who) if who else None,
        "addressed": who.addressed if who else None,
    }


def _run(
    argv: list[str],
    *,
    timeout: int,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
        cwd=cwd,
    )


def client_env() -> dict[str, str]:
    """This process's environment, pointed at the home the connection lives in.

    A shell the person opened did not inherit the desktop app's `PAPAYA_AGENT_HOME`,
    so a client started from it looks in the wrong home and says "not connected"
    while `status()` says connected. Every call into the client goes through this.
    Whatever uv runs it (`uv tool run`, the npm shim) shows no progress bars.
    """
    env = dict(os.environ)
    env[UV_NO_PROGRESS] = "1"
    found = _best()
    if found is not None:
        env[CLIENT_HOME_ENV] = str(found[1])
    return env


def installer() -> str | None:
    """How this machine would get the client: ``installed``, ``npx``, ``uv``, or None."""
    if installed():
        return "installed"
    if shutil.which("npx"):
        return "npx"
    if shutil.which("uv"):
        return "uv"
    return None


# ── keeping the person's client current ─────────────────────────────────────

_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
#: `>=0.18.1,<0.19.0` in pyproject: the floor, when there is no lock to read.
_FLOOR = re.compile(re.escape(CLIENT_PACKAGE) + r"\s*>=\s*(\d+\.\d+\.\d+)")
#: `papaya-agent-client v0.17.0` in `uv tool list`.
_UV_TOOL = re.compile(r"^" + re.escape(CLIENT_PACKAGE) + r"\s+v?(\d+\.\d+\.\d+)", re.MULTILINE)


def _version_key(version: str) -> tuple[int, ...] | None:
    match = _VERSION.search(version)
    return tuple(int(part) for part in match.groups()) if match else None


def locked_client_version(root: str | Path | None = None) -> str | None:
    """The client version this checkout locks: `uv.lock`, else the pyproject floor.

    The version `ppy serve` embeds, so the person's own `papaya-agent` (which runs
    the connect, the plugin's hooks and the `papaya` MCP server) behaves the same.
    """
    import tomllib

    if root is None:
        from papaya_agent_runtime import readiness

        root = readiness.checkout_root()
    root = Path(root)
    try:
        lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        lock = {}
    for package in lock.get("package") or []:
        if isinstance(package, dict) and package.get("name") == CLIENT_PACKAGE:
            version = str(package.get("version") or "")
            if _version_key(version) is not None:
                return version
    try:
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return None
    floor = _FLOOR.search(text)
    return floor.group(1) if floor else None


def uv_install_argv(
    version: str | None = None, *, force: bool = False, quiet: bool = True
) -> list[str]:
    """`uv tool install --quiet [--force] papaya-agent-client==<locked>`; unpinned with no
    lock. ``quiet=False`` is the command to hand a person, who wants to see it work."""
    version = version or locked_client_version()
    spec = f"{CLIENT_PACKAGE}=={version}" if version else CLIENT_PACKAGE
    return [
        "uv",
        "tool",
        "install",
        *([UV_QUIET] if quiet else []),
        *(["--force"] if force else []),
        spec,
    ]


def _said_last(proc: subprocess.CompletedProcess[str], lines: int = 3) -> str:
    """The last few lines a captured command printed (stderr after stdout): why it failed."""
    said = [
        line.strip()
        for line in f"{proc.stdout or ''}\n{proc.stderr or ''}".splitlines()
        if line.strip()
    ]
    return " / ".join(said[-lines:])


def client_version(path: str, *, run: Any = None) -> str | None:
    """The version of the `papaya-agent` at ``path``, or None when it will not say.

    Its own `--version` first; a client that has none (0.18 and older refuse the
    flag) is read from `uv tool list`, which is how `setup` and the npm shim install it.
    """
    runner = run or _run
    try:
        proc = runner([path, "--version"], timeout=PROBE_TIMEOUT)
        if proc.returncode == 0:
            match = _VERSION.search(proc.stdout or "")
            if match is not None:
                return match.group(0)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        proc = runner(["uv", "tool", "list"], timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    listed = _UV_TOOL.search(proc.stdout or "") if proc.returncode == 0 else None
    return listed.group(1) if listed else None


def keep_client_current(*, root: str | Path | None = None, run: Any = None) -> dict:
    """Bring the person's own `papaya-agent` up to the version this runtime locks.

    Never raises. ``state`` is what happened, ``line`` the one thing to say (None
    when there is nothing worth saying):

    - ``absent`` — no client of their own; connect installs the locked one.
    - ``unknown`` — the client or the lock would not say its version; left alone.
    - ``current`` / ``newer`` — nothing to do; a newer client is theirs to keep.
    - ``updated`` — it was older and was reinstalled at the locked version.
    - ``failed`` — it was older and the reinstall failed; ``command`` is what to run.

    An older client silently falls back to the numbered prompts and the HTTP log
    lines at connect (2026-09-24: 0.17.0 on the PATH under a runtime locking 0.18.1),
    because installing it once never upgrades it.
    """
    runner = run or _run
    path = installed()
    if path is None:
        return {"state": "absent", "line": None}
    locked = locked_client_version(root)
    have = client_version(path, run=runner)
    result: dict[str, Any] = {"path": path, "before": have, "locked": locked}
    have_key = _version_key(have or "")
    locked_key = _version_key(locked or "")
    if have_key is None or locked_key is None:
        return {**result, "state": "unknown", "line": None}
    if have_key == locked_key:
        return {**result, "state": "current", "line": None}
    if have_key > locked_key:
        return {**result, "state": "newer", "line": None}
    command = uv_install_argv(locked, force=True, quiet=False)
    why = ""
    try:
        proc = runner(uv_install_argv(locked, force=True), timeout=PROBE_TIMEOUT * 4)
        ok = proc.returncode == 0
        if not ok:
            why = _said_last(proc)
    except (OSError, subprocess.TimeoutExpired) as exc:
        ok = False
        why = str(exc)
    if ok:
        after = client_version(installed() or path, run=runner)
        after_key = _version_key(after or "")
        ok = after_key is None or after_key >= locked_key
    if ok:
        return {
            **result,
            "state": "updated",
            "line": f"Updated papaya-agent {have} → {locked}",
        }
    return {
        **result,
        "state": "failed",
        "command": command,
        "detail": why,
        "line": (
            f"papaya-agent {have} is older than {locked} and could not be updated"
            + (f" ({why})" if why else "")
            + f": run {' '.join(command)}"
        ),
    }


#: `connect --quiet` (client 0.18.1): the sign-in, its questions and the result, without
#: the client's progress chatter. An older client refuses a flag it does not know.
QUIET_FLAG = "--quiet"
_QUIET = re.compile(r"(?<![\w-])--quiet(?![\w-])")


def connect_takes_quiet(base: list[str]) -> bool:
    """Whether the client ``base`` runs offers ``connect --quiet``: its own help says so.

    A help that cannot be read (no client yet, a timeout) counts as no, so an old or
    unreachable client is run exactly as before.
    """
    try:
        proc = _run([*base, "connect", "--help"], timeout=PROBE_TIMEOUT, env=client_env())
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and _QUIET.search(proc.stdout or "") is not None


def connect_argv(
    *,
    harness: str = "claude",
    workspace: str | None = None,
    agent: str | None = None,
    device: bool = False,
    no_browser: bool = False,
    quiet: bool = False,
) -> list[str] | None:
    """The exact command that establishes the connection, or None with no way to run one.

    Prefers an installed `papaya-agent`; then the npm shim (`npx papaya-agent`), which
    installs the client as a side effect so the next run takes the first branch; then
    the same client through `uv` for a machine with no Node. ``quiet`` adds
    :data:`QUIET_FLAG` only when that client offers it (:func:`connect_takes_quiet`).
    """
    how = installer()
    if how == "installed":
        base = [str(installed())]
    elif how == "npx":
        base = list(BOOTSTRAP)
    elif how == "uv":
        base = list(UV_BOOTSTRAP)
    else:
        return None
    argv = [*base, "connect", "--harness", harness]
    if workspace:
        argv += ["--workspace", workspace]
    if agent:
        argv += ["--agent", agent]
    if device:
        argv.append("--device")
    if no_browser:
        argv.append("--no-browser")
    if quiet and connect_takes_quiet(base):
        argv.append(QUIET_FLAG)
    return argv


def _stream(argv: list[str], *, timeout: int, echo: Any) -> tuple[int, list[str]]:
    """Run ``argv`` with no stdin, echoing each line as it comes, and keep the lines.

    No stdin is what makes the client answer with its choices instead of prompting a
    terminal nobody is at; echoing is what puts the sign-in link in front of the person
    while the flow waits for them, rather than after it has timed out.
    """
    import threading

    env = client_env()
    # The client is Python writing to a pipe, so it buffers: the sign-in link sat in
    # that buffer until the flow ended, and on a timeout it was never seen at all.
    # The variable passes through `npx` and `uv` to the interpreter.
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    lines: list[str] = []

    def pump() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            lines.append(line)
            if echo is not None:
                print(line, file=echo, flush=True)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        reader.join(timeout=5)
        # What was printed before the wait ran out (the sign-in link) rides the error.
        exc.output = "\n".join(lines)
        raise
    reader.join(timeout=5)
    return code, lines


def _attached(argv: list[str], *, timeout: int, echo: Any) -> tuple[int, list[str]]:
    """Run ``argv`` on the person's terminal: their stdin, and output shown as it comes.

    The client asks which workspace and which agent only when its stdin is a
    terminal, so inheriting stdin is what puts both questions inside the one
    sign-in. Output still passes through here, a chunk at a time rather than a line
    at a time, so a question with no newline after it (``Agent number:``) shows
    before the answer is typed, and a copy is kept to read what the client said.

    On a POSIX terminal the client runs under a pseudo-terminal (:func:`_on_pty`),
    because it offers its arrow-key pickers only when its stdout is a terminal too.
    """
    if _pty_wanted():
        return _on_pty(argv, timeout=timeout, echo=echo)
    import codecs
    import threading

    env = client_env()
    env["PYTHONUNBUFFERED"] = "1"
    out = echo if echo is not None else sys.stdout
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        argv,
        stdin=None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    said: list[str] = []

    def pump() -> None:
        assert proc.stdout is not None
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        fd = proc.stdout.fileno()
        while True:
            chunk = os.read(fd, 4096)
            text = decoder.decode(chunk, final=not chunk)
            if text:
                said.append(text)
                print(text, end="", file=out, flush=True)
            if not chunk:
                return

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        reader.join(timeout=5)
        exc.output = "".join(said)
        raise
    except BaseException:
        # Ctrl-C at the client's question: the client got it too; do not leave it behind.
        proc.kill()
        raise
    reader.join(timeout=5)
    return code, "".join(said).splitlines()


def _pty_wanted(stdin: Any = None) -> bool:
    """Run the client under a pseudo-terminal: POSIX, with a person at the keyboard."""
    if os.name != "posix":
        return False
    stream = stdin if stdin is not None else sys.stdin
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


#: Escape sequences the client's pickers draw with: CSI, OSC, and the two-byte ones.
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")


def _plain(text: str) -> str:
    """What a terminal would leave on screen, near enough to read lines from.

    Escapes dropped, and a line redrawn after a carriage return is its last drawing.
    """
    lines = []
    for line in _ANSI.sub("", text).replace("\r\n", "\n").split("\n"):
        drawn = [part for part in line.split("\r") if part]
        lines.append(drawn[-1] if drawn else "")
    return "\n".join(lines)


def _take_terminal() -> None:
    """In the child, after `setsid`: make the pty its controlling terminal, so Ctrl-C
    typed while the client is not reading keys still reaches it as SIGINT."""
    import fcntl
    import termios

    with contextlib.suppress(OSError):
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _copy_window_size(source: int, target: int) -> None:
    import fcntl
    import termios

    with contextlib.suppress(OSError):
        size = fcntl.ioctl(source, termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(target, termios.TIOCSWINSZ, size)


def _stop(proc: subprocess.Popen | None) -> None:
    """Kill the client and everything it started (`npx`/`uv` run it as a grandchild)."""
    import signal

    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5)


def _on_pty(
    argv: list[str], *, timeout: int, echo: Any, stdin_fd: int | None = None
) -> tuple[int, list[str]]:
    """Run ``argv`` on a pseudo-terminal, relaying the person's keys to it and its output back.

    The client draws its arrow-key workspace and agent pickers only when both its
    stdin and stdout are terminals; with stdout piped it falls back to numbered
    prompts. The pty is that terminal, and it still passes every byte through here,
    so what the client said is kept. The person's terminal is raw for the duration —
    each key goes straight to the client, whose pty echoes it — and is restored on
    every way out. ``stdin_fd`` is the terminal to read keys from (a test's pty).
    """
    import codecs
    import pty
    import select
    import signal
    import termios
    import time
    import tty

    env = client_env()
    env["PYTHONUNBUFFERED"] = "1"
    out = echo if echo is not None else sys.stdout
    keys = sys.stdin.fileno() if stdin_fd is None else stdin_fd
    master, slave = pty.openpty()
    _copy_window_size(keys, slave)
    saved = termios.tcgetattr(keys)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    said: list[str] = []
    proc: subprocess.Popen | None = None
    resized: Any = None
    interrupted = False

    def show(chunk: bytes) -> None:
        text = decoder.decode(chunk, final=not chunk)
        if text:
            said.append(text)
            print(text, end="", file=out, flush=True)

    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            argv,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            start_new_session=True,
            preexec_fn=_take_terminal,  # noqa: PLW1509 - setup runs no threads here
        )
        os.close(slave)
        slave = -1
        try:
            resized = signal.signal(signal.SIGWINCH, lambda *_: _copy_window_size(keys, master))
        except ValueError:  # not the main thread: the size stays as it started
            resized = None
        tty.setraw(keys, termios.TCSANOW)
        deadline = time.monotonic() + timeout
        reading = True
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            watched = [master, keys] if reading else [master]
            ready, _, _ = select.select(watched, [], [], min(left, 0.1))
            if master in ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:  # EIO: every copy of the client's side is closed
                    chunk = b""
                if not chunk:
                    break
                show(chunk)
            elif proc.poll() is not None:
                # Exited, and nothing left to read (a descendant may still hold the pty).
                break
            if keys in ready:
                typed = os.read(keys, 1024)
                if typed:
                    interrupted = interrupted or b"\x03" in typed
                    os.write(master, typed)
                else:
                    reading = False
        show(b"")
        code = proc.wait(timeout=max(deadline - time.monotonic(), 1))
    except subprocess.TimeoutExpired as exc:
        _stop(proc)
        exc.output = _plain("".join(said))
        raise
    except BaseException:
        _stop(proc)
        raise
    finally:
        termios.tcsetattr(keys, termios.TCSADRAIN, saved)
        if resized is not None:
            signal.signal(signal.SIGWINCH, resized)
        os.close(master)
        if slave >= 0:
            os.close(slave)
    if interrupted and code != 0:
        # Ctrl-C at the client's question ends setup, as it did before the pty.
        raise KeyboardInterrupt
    return code, _plain("".join(said)).splitlines()


#: Where a line of the client's starts. The answer typed at `Workspace number: ` is the
#: terminal's echo, not the client's output, so in the copy the next line runs on
#: from the question.
_STARTS = r"(?:^|number: )"
#: What the client prints once a connection is recorded:
#: `Connected as Middle Manager (@handle) in Papaya HQ.`, and last of all the same
#: with `. Open Claude Code in any repository …` after the workspace.
_CONNECTED = re.compile(_STARTS + r"Connected as .+? in (?P<workspace>.+?)\.(?: Open .*)?$")
#: The client's browser flow names a lone candidate instead of asking: `Agent: <label>`.
_ONLY_AGENT = re.compile(_STARTS + r"Agent: \S")
#: …and lists several before asking: `Choose an agent:` then a number, or the
#: arrow-key list's `? Choose an agent` on a terminal.
_ASKED_AGENT = re.compile(r"Choose an? agent\b")


def connect(
    *,
    harness: str = "claude",
    workspace: str | None = None,
    agent: str | None = None,
    device: bool = False,
    no_browser: bool = False,
    timeout: int = CONNECT_TIMEOUT,
    echo: Any = None,
    interactive: bool = False,
    quiet: bool = False,
) -> dict:
    """Install the client if it is missing, run its connect flow, and say what happened.

    Never raises. The flow opens a sign-in link and waits for the person to click
    Approve: that is the one moment a person is in the loop, and it is a browser
    click, never a command they type. ``interactive`` runs it on the person's
    terminal, so the client itself asks which workspace and agent inside that one
    sign-in; without it, the client has no stdin and answers with its choices.
    ``quiet`` asks the client for less output where it offers that (`ppy setup`).

    ``ok`` means this run connected: the client exited 0 *and* recorded a
    connection. A connection that was already there says nothing about this run,
    so a switch that fails part-way is a failure, never the old agent reported as
    new. On success, ``before`` is who was connected before (or None), ``agent_choice``
    is ``only`` when the client took the one agent there was and ``asked`` when it
    offered several, and ``workspace`` is the name the client connected in, when it
    said. ``reason`` on a failure is what the caller branches on:

    - ``choose`` — the account has several workspaces or agents; ``kind``, ``flag`` and
      ``choices`` say which, so the person picks in conversation and it is re-run with
      that flag;
    - ``timeout`` — nobody approved in time; ``link`` is the sign-in link when one was
      printed;
    - ``no_installer`` — neither Node (`npx`) nor `uv` is on this machine;
    - ``unavailable``, ``failed``, ``declined`` — as the words say, with ``detail``.
    """
    argv = connect_argv(
        harness=harness,
        workspace=workspace,
        agent=agent,
        device=device,
        no_browser=no_browser,
        quiet=quiet,
    )
    if argv is None:
        return {
            "ok": False,
            "reason": "no_installer",
            "detail": "neither `npx` (Node) nor `uv` is on this machine to install the client",
            "command": None,
        }
    how = installer()
    before = identity()
    mark = _connection_mark()
    lines: list[str] = []
    try:
        if interactive:
            code, lines = _attached(argv, timeout=timeout, echo=echo)
        else:
            code, lines = _stream(argv, timeout=timeout, echo=echo)
    except subprocess.TimeoutExpired as exc:
        printed = exc.output if isinstance(exc.output, str) else ""
        return {
            "ok": False,
            "reason": "timeout",
            "detail": f"the sign-in was still waiting for Approve after {timeout}s",
            "link": _first_link(printed.splitlines()),
            "command": argv,
        }
    except OSError as exc:
        return {"ok": False, "reason": "unavailable", "detail": str(exc), "command": argv}
    for line in lines:
        choice = _CHOICE.search(line.strip())
        if choice is not None:
            return {
                "ok": False,
                "reason": "choose",
                "kind": choice["kind"],
                "flag": choice["flag"],
                "choices": [c.strip() for c in choice["choices"].split(";") if c.strip()],
                "detail": line.strip(),
                "command": argv,
            }
    after = status()
    now = _connection_mark()
    # A client too old to stamp the pin leaves ("", "") both times; exit 0 is all there is.
    recorded = now is not None and (mark is None or now != mark or now == ("", ""))
    if code == 0 and after["state"] == "connected" and recorded:
        result: dict[str, Any] = {
            "ok": True,
            "status": after,
            "command": argv,
            "via": how,
            "before": asdict(before) if before is not None else None,
            "agent_choice": _agent_choice(lines),
            "workspace": _workspace_named(lines),
        }
        if how == "uv" and not installed():
            # The npm shim keeps the client on the PATH after a connect; do the same.
            try:
                kept = _run(uv_install_argv(), timeout=PROBE_TIMEOUT * 4)
                result["installed"] = kept.returncode == 0
                if kept.returncode != 0:
                    result["install_detail"] = _said_last(kept)
            except (OSError, subprocess.TimeoutExpired) as exc:
                result["installed"] = False
                result["install_detail"] = str(exc)
        return result
    tail = [line for line in lines if line.strip()]
    return {
        "ok": False,
        "reason": "declined" if code == 0 else "failed",
        "detail": tail[-1] if tail else f"exit {code}",
        "link": _first_link(lines),
        "status": after,
        "command": argv,
    }


def _agent_choice(lines: list[str]) -> str | None:
    """``only`` when the client took the lone agent, ``asked`` when it listed several.

    None when it said neither: a device-code connect chooses in the app.
    """
    for line in lines:
        text = line.strip()
        if _ASKED_AGENT.search(text):
            return "asked"
        if _ONLY_AGENT.search(text):
            return "only"
    return None


def _workspace_named(lines: list[str]) -> str | None:
    for line in lines:
        match = _CONNECTED.search(line.strip())
        if match is not None:
            return match["workspace"]
    return None


def _first_link(lines: list[str]) -> str | None:
    """The sign-in link the client printed for the person — never an API call it logged."""
    for line in lines:
        if "HTTP Request:" in line:
            continue
        match = _LINK.search(line)
        if match is not None:
            return match.group(0)
    return None


def context(*, refresh: bool = False) -> dict | None:
    """The agent's durable context: persona, objective, rules, memories.

    Returned verbatim from `papaya-agent context --json`. The harness re-injects
    this on its own through the plugin's hooks, so this exists for the cases the
    hooks do not cover — a doctor run, a Codex session, a preflight that wants to
    say who it is before the first hook fires. Returns None when not connected or
    when the client cannot answer.
    """
    from papaya_agent_runtime.manager.launch import papaya_agent_command

    if installed() is None and _best() is None:
        return None
    env = client_env()
    # The client `ppy serve` embeds, not whichever `papaya-agent` is first on PATH: an
    # older one cannot read the desktop app's connection and says "connect first".
    argv = [*papaya_agent_command(env), "context", "--json"]
    if refresh:
        argv.append("--refresh")
    try:
        proc = _run(argv, timeout=PROBE_TIMEOUT, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def agent_env() -> dict[str, str]:
    """The API url, workspace and token a `ppy serve` job would carry, from this connection.

    What lets a session read and write work items the way serve's rounds do
    (`rounds.Rounds._env_from_connection` is the same three values). Empty when this
    machine is not connected.
    """
    found = _best()
    if found is None:
        return {}
    who, home = found
    config = _read_config(home)
    agent = (config.get("agents") or {}).get(who.agent_id) or {}
    return {
        "PAPAYA_API_URL": os.environ.get("PAPAYA_API_URL") or str(config.get("server_url") or ""),
        "PAPAYA_WORKSPACE_ID": str(agent.get("workspace_id") or ""),
        "PAPAYA_AGENT_TOKEN": str(agent.get("client_token") or ""),
    }


def agent_api() -> Any | None:
    """An agent-token API client for this connection, or ``None`` when not connected.

    The same client `ppy serve` builds for its listener, for the things a session says
    as the agent between turns (the owner's DM). Built from the client's own config, so
    it needs no network to answer "is there one".
    """
    found = _best()
    if found is None:
        return None
    who, home = found
    config = _read_config(home)
    agent = (config.get("agents") or {}).get(who.agent_id)
    if not agent:
        return None
    from papaya_agent_client.api_client import AgentTokenApi

    return AgentTokenApi(config, agent)


# ── Papaya tools in an interactive session ──────────────────────────────────

#: The MCP server name, the same one `ppy serve`'s turns load.
SESSION_SERVER = "papaya"
#: Where Claude Code keeps a project's local-scope MCP servers.
CLAUDE_USER_CONFIG_ENV = "PPY_CLAUDE_USER_CONFIG"


def _claude_user_config() -> Path:
    override = os.environ.get(CLAUDE_USER_CONFIG_ENV)
    return Path(override) if override else Path.home() / ".claude.json"


def session_server(root: str | Path) -> dict | None:
    """The Papaya MCP server a Claude Code session in ``root`` loads, if any."""
    try:
        data = json.loads(_claude_user_config().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    projects = data.get("projects") or {}
    for key in _project_keys(root):
        server = ((projects.get(key) or {}).get("mcpServers") or {}).get(SESSION_SERVER)
        if isinstance(server, dict):
            return server
    return None


def _project_keys(root: str | Path) -> list[str]:
    """The keys Claude Code may file ``root``'s local settings under.

    The directory itself, and the main checkout of the repository it belongs to: a
    git worktree's local-scope servers are kept under its main checkout.
    """
    resolved = Path(root).resolve()
    keys = [str(resolved)]
    try:
        proc = _run(
            ["git", "-C", str(resolved), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            timeout=PROBE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return keys
    common = proc.stdout.strip() if proc.returncode == 0 else ""
    if common:
        main = str(Path(common).parent.resolve())
        if main not in keys:
            keys.append(main)
    return keys


def session_tools_ready(root: str | Path) -> bool:
    """Would a Claude Code session opened in ``root`` have this agent's Papaya tools?

    Configured, and still runnable: the client's interpreter path moves when the
    runtime's environment is rebuilt, and a server whose command is gone fails
    silently at session start.
    """
    server = session_server(root)
    if server is None:
        return False
    command = str(server.get("command") or "")
    return not command.startswith("/") or Path(command).exists()


def install_session_tools(root: str | Path, *, run=None) -> dict:
    """Give Claude Code sessions in ``root`` the Papaya tools this machine is connected as.

    `ppy serve`'s headless turns get them by passing the client's `runner-config`
    server with `--mcp-config`. A session a person opens in the runtime directory
    loads only its own Claude Code configuration, so on 2026-09-17 a manager session
    connected as the engineering agent could not read or comment on a work item.
    This writes the same server into Claude Code's local scope for this directory
    (never a committed file), replacing any stale copy. A session already running
    loads it after `/mcp` reconnects or the session restarts. Never raises.
    """
    from papaya_agent_runtime.manager.launch import papaya_agent_command

    runner = run or _run
    root = str(Path(root).resolve())
    if status()["state"] != "connected":
        return {
            "ok": False,
            "reason": "not_connected",
            "detail": "this machine is not connected to a Papaya agent: `ppy papaya connect`",
        }
    claude = shutil.which("claude")
    if claude is None:
        return {"ok": False, "reason": "no_claude", "detail": "the `claude` CLI is not on PATH"}
    env = client_env()
    command = [
        *papaya_agent_command(env),
        "mcp",
        "runner-config",
        "--harness",
        "claude-code",
        "--working-directory",
        root,
    ]
    try:
        proc = runner(command, timeout=PROBE_TIMEOUT * 4, env=env, cwd=root)
        server = (
            json.loads(proc.stdout)["mcpServers"][SESSION_SERVER] if proc.returncode == 0 else None
        )
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as exc:
        return {"ok": False, "reason": "client_failed", "detail": f"runner-config: {exc}"}
    if server is None:
        said = (proc.stderr or proc.stdout or "").strip().splitlines()
        last = said[-1] if said else "no output"
        return {
            "ok": False,
            "reason": "client_failed",
            "detail": f"runner-config exited {proc.returncode}: {last}",
        }
    try:
        runner(
            [claude, "mcp", "remove", SESSION_SERVER, "--scope", "local"],
            timeout=PROBE_TIMEOUT,
            cwd=root,
        )
        added = runner(
            [claude, "mcp", "add-json", SESSION_SERVER, json.dumps(server), "--scope", "local"],
            timeout=PROBE_TIMEOUT,
            cwd=root,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "reason": "claude_failed", "detail": str(exc)}
    if added.returncode != 0:
        said = (added.stderr or added.stdout or "").strip().splitlines()
        return {
            "ok": False,
            "reason": "claude_failed",
            "detail": said[-1] if said else f"claude mcp add-json exited {added.returncode}",
        }
    return {
        "ok": True,
        "addressed": status()["addressed"],
        "detail": (
            "Papaya tools are configured for Claude Code sessions in this directory; a "
            "session already open loads them after `/mcp` or a restart"
        ),
    }


__all__ = [
    "AGENT_PERSONAL",
    "AGENT_SHARED",
    "BOOTSTRAP",
    "CLI",
    "CLIENT_HOME_ENV",
    "CONNECT_TIMEOUT",
    "HOME_ENV",
    "MEMORY_PAPAYA",
    "MEMORY_REPO_NOTES_ONLY",
    "QUIET_FLAG",
    "AgentKind",
    "Identity",
    "agent_kind_of",
    "agent_kinds_path",
    "candidate_homes",
    "client_home",
    "config_path",
    "connect",
    "connect_argv",
    "connect_takes_quiet",
    "context",
    "identity",
    "installed",
    "keep_client_current",
    "known_agent_kind",
    "locked_client_version",
    "remember_agent_kind",
    "signed_in",
    "status",
]
