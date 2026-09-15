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
- **Everything else lives in the harness.** Reading work items, posting comments,
  proposing memories and searching the workspace are MCP tool calls the agent makes
  directly; shelling out to re-implement them here would be slower and lossier.

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

#: Task-env keys that record which Papaya work item a local task belongs to.
WORK_ITEM_KEY = "papaya_work_item"
WORK_ITEM_URL_KEY = "papaya_work_item_url"
WORK_ITEM_TITLE_KEY = "papaya_work_item_title"


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


def _run(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


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
    path = installed()
    if path is None:
        return None
    argv = [path, "context", "--json"]
    if refresh:
        argv.append("--refresh")
    try:
        proc = _run(argv, timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


# ── Linking local tasks to Papaya work items ────────────────────────────────


def link_task(
    conn,
    task_id: int,
    *,
    work_item: str,
    url: str = "",
    title: str = "",
) -> None:
    """Record which Papaya work item a dispatched task belongs to.

    Not every task earns a work item — most are a step inside one, and minting an
    item per step is the noise this runtime exists to avoid. The link exists so the
    tasks that *do* belong to tracked work carry it into the pull request body and
    the board, instead of the connection living only in the session's head.
    """
    from papaya_agent_runtime.state import store

    store.set_task_env(conn, task_id, WORK_ITEM_KEY, work_item, source="papaya")
    if url:
        store.set_task_env(conn, task_id, WORK_ITEM_URL_KEY, url, source="papaya")
    if title:
        store.set_task_env(conn, task_id, WORK_ITEM_TITLE_KEY, title, source="papaya")


def task_link(conn, task_id: int) -> dict | None:
    """The Papaya work item a task belongs to, or None when it is unlinked."""
    from papaya_agent_runtime.state import store

    item = store.get_task_env(conn, task_id, WORK_ITEM_KEY)
    if not item:
        return None
    return {
        "work_item": item,
        "url": store.get_task_env(conn, task_id, WORK_ITEM_URL_KEY) or "",
        "title": store.get_task_env(conn, task_id, WORK_ITEM_TITLE_KEY) or "",
    }


def link_sentence(link: dict | None) -> str:
    """How a work-item link reads in a pull request body or a report.

    Describes the item rather than citing a bare identifier, because a reader
    outside this workspace cannot look one up.
    """
    if not link:
        return ""
    title = link.get("title") or ""
    url = link.get("url") or ""
    subject = f'the Papaya work item "{title}"' if title else "its Papaya work item"
    if url:
        return f"Tracked in Papaya under {subject} ({url})."
    return f"Tracked in Papaya under {subject}."


__all__ = [
    "BOOTSTRAP",
    "CLI",
    "CLIENT_HOME_ENV",
    "CONNECT_TIMEOUT",
    "HOME_ENV",
    "WORK_ITEM_KEY",
    "WORK_ITEM_TITLE_KEY",
    "WORK_ITEM_URL_KEY",
    "Identity",
    "candidate_homes",
    "client_home",
    "config_path",
    "connect",
    "connect_argv",
    "context",
    "identity",
    "installed",
    "link_sentence",
    "link_task",
    "signed_in",
    "status",
    "task_link",
]
