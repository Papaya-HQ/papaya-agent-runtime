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

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

#: The command `papaya-agent connect` installs onto the PATH.
CLI = "papaya-agent"
#: How to reach the client when it is not installed yet. The npm package is a shim
#: that finds or downloads `uv` and then installs the Python client for good.
BOOTSTRAP = ("npx", "--yes", "papaya-agent")
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
    """The path to `papaya-agent`, or None when it is not on the PATH yet."""
    return shutil.which(CLI)


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


def _best() -> tuple[Identity, Path] | None:
    """The connection to act as, across every place one could have been made.

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
    _, who, home = found[0]
    return who, home


def identity() -> Identity | None:
    """The agent this machine is pinned to, or None when nothing is connected."""
    found = _best()
    return found[0] if found is not None else None


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
    - ``absent`` — no client; `npx papaya-agent connect` would install one.
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
    """
    env = dict(os.environ)
    found = _best()
    if found is not None:
        env[CLIENT_HOME_ENV] = str(found[1])
    return env


def connect_argv(*, harness: str = "claude") -> list[str]:
    """The exact command that establishes the connection.

    Prefers an installed `papaya-agent`; falls back to the npm shim, which installs
    the client as a side effect so the next run takes the first branch.
    """
    path = installed()
    base = [path] if path else list(BOOTSTRAP)
    return [*base, "connect", "--harness", harness]


def connect(*, harness: str = "claude", timeout: int = CONNECT_TIMEOUT) -> dict:
    """Run the connect flow and report what happened, without ever raising.

    The flow is interactive by design: it opens a sign-in link and waits for the
    person to click Approve. That is the one moment a person is in the loop, and it
    is a browser click rather than a command they have to type.
    """
    argv = connect_argv(harness=harness)
    try:
        proc = _run(argv, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "reason": "timeout",
            "detail": f"the connect flow was still waiting after {timeout}s",
            "command": argv,
        }
    except OSError as exc:
        return {"ok": False, "reason": "unavailable", "detail": str(exc), "command": argv}
    after = status()
    if after["state"] == "connected":
        return {"ok": True, "status": after, "command": argv}
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return {
        "ok": False,
        "reason": "declined" if proc.returncode == 0 else "failed",
        "detail": detail[-1] if detail else f"exit {proc.returncode}",
        "status": after,
        "command": argv,
    }


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
    "AgentKind",
    "Identity",
    "agent_kind_of",
    "agent_kinds_path",
    "candidate_homes",
    "client_home",
    "config_path",
    "connect",
    "connect_argv",
    "context",
    "identity",
    "installed",
    "known_agent_kind",
    "remember_agent_kind",
    "signed_in",
    "status",
]
