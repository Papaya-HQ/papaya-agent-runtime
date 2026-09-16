"""Can this runtime actually do work, and if not, whose problem is it?

`ppy doctor` prints everything it knows and leaves the reader to decide what it
means. That is the right shape for a diagnostic and the wrong shape for the
question that actually matters on a machine somebody just connected: *is this
thing able to take work, right now?*

The failure this exists for is silent. A Papaya agent connected to a runtime that
was never configured looks completely healthy from the workspace: the connection is
live, the listener is running, events are consumed. The first job then starts a
harness in a home with no config, no registered repositories and no worker ceiling,
produces nothing, and writes a zero-byte log nobody reads. Nothing is broken enough
to raise, so nothing tells the person who owns the connection that their agent
cannot work.

So this module answers one question with three possible answers — `ready`,
`degraded` (can work, something is missing), `blocked` (cannot work) — and for
anything short of ready, names each problem, what closes it, and **who has to close
it**: the runtime itself, or a person. That last distinction is the point. A missing
config is the runtime's own job and it should just do it; an unauthenticated harness
is not, and saying so is the difference between a useful message and a complaint.

Every check reads local state only, so a verdict is instant and works offline. The
fingerprint exists so the same problem is reported once rather than on every wake.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

#: The runtime can fix this itself, without asking anybody.
RUNTIME = "runtime"
#: This needs a person: a credential, a decision, or a choice only they can make.
USER = "user"

#: Cannot take work at all.
BLOCKED = "blocked"
#: Can take work, but something is missing that will bite later.
DEGRADED = "degraded"
#: Nothing in the way.
READY = "ready"


@dataclass(frozen=True)
class Problem:
    """One thing standing between this runtime and doing useful work."""

    code: str
    summary: str
    fix: str
    owner: str = RUNTIME
    blocking: bool = True


@dataclass
class Readiness:
    """The verdict, and everything behind it."""

    state: str = READY
    problems: list[Problem] = field(default_factory=list)
    checked_at: str = ""

    @property
    def blockers(self) -> list[Problem]:
        return [p for p in self.problems if p.blocking]

    @property
    def warnings(self) -> list[Problem]:
        return [p for p in self.problems if not p.blocking]

    @property
    def fingerprint(self) -> str:
        """A stable id for *this set of problems*.

        Reporting is keyed on it, so an unchanged situation is said once and a
        changed one is said again. Codes only: the wording may improve without
        making the runtime repeat itself to the same person.
        """
        codes = ",".join(sorted(p.code for p in self.problems))
        return hashlib.sha256(codes.encode("utf-8")).hexdigest()[:16]

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "checked_at": self.checked_at,
            "fingerprint": self.fingerprint,
            "problems": [asdict(p) for p in self.problems],
        }


#: What gets a harness signed in, when discovery has no opinion of its own.
_SIGN_IN = {
    "claude": "run `claude auth login`",
    "codex": "run `codex login`",
}


def _configured_providers() -> dict[str, str]:
    """The provider each role is configured to use, or nothing if unreadable."""
    from papaya_agent_runtime.config import ConfigError, load_config

    try:
        cfg = load_config()
    except (ConfigError, OSError):  # already reported by _config_problems
        return {}
    return {"driver": cfg.manager.provider, "worker": cfg.worker.provider}


def _unusable_provider_problem(report: dict, usable: list[str]) -> Problem | None:
    """A role pinned to a harness this machine cannot launch.

    Distinct from ``no_harness``: something *is* signed in here, so the runtime
    looks healthy and a dispatch fails only at the moment it matters. Switching
    to the harness that happens to work would be the runtime quietly overriding a
    choice a person made, so this stays theirs to close.
    """
    if not usable:
        return None  # `no_harness` already says it, and says it better
    stranded = {
        role: provider
        for role, provider in _configured_providers().items()
        if provider not in usable
    }
    if not stranded:
        return None
    details = {h["name"]: (h.get("detail") or "") for h in report.get("harnesses", [])}
    roles = ", ".join(f"{role} ({provider})" for role, provider in sorted(stranded.items()))
    fixes = sorted(
        {
            details.get(p) or _SIGN_IN.get(p, f"install and sign in to {p}")
            for p in stranded.values()
        }
    )
    return Problem(
        code="provider_unusable",
        summary=(
            f"configured to use a harness this machine cannot launch: {roles}; "
            f"usable here: {', '.join(usable)}"
        ),
        fix="; ".join(fixes) + " — or `ppy config models` to choose a harness that works here",
        owner=USER,
    )


def _harness_problems(problems: list[Problem]) -> None:
    from papaya_agent_runtime.setup.discovery import discover, usable_harnesses

    try:
        report = discover()
    except Exception:  # noqa: BLE001 - a readiness check must never be the thing that breaks
        return
    usable = usable_harnesses(report)
    if not usable:
        problems.append(
            Problem(
                code="no_harness",
                summary="no signed-in coding harness, so no worker can be launched",
                fix="sign in to Claude Code (`claude`) or Codex (`codex`) on this machine",
                owner=USER,
            )
        )
    stranded = _unusable_provider_problem(report, usable)
    if stranded is not None:
        problems.append(stranded)
    missing = [c["name"] for c in report.get("companions", []) if not c.get("available")]
    if missing:
        problems.append(
            Problem(
                code="companions_missing",
                summary=f"companion tools not provisioned: {', '.join(sorted(missing))}",
                fix="`ppy tools install` — the documented fallbacks work meanwhile",
                blocking=False,
            )
        )


def _config_problems(problems: list[Problem]) -> None:
    from papaya_agent_runtime.config import ConfigError, load_config
    from papaya_agent_runtime.paths import config_path

    if not config_path().exists():
        problems.append(
            Problem(
                code="no_config",
                summary=(
                    "this runtime has never been set up: no driver profile and no worker ceiling"
                ),
                fix="`ppy setup` — `ppy serve` does this itself before it starts listening",
            )
        )
        return
    try:
        cfg = load_config()
    except ConfigError as exc:
        problems.append(
            Problem(
                code="config_invalid",
                summary=f"the configuration cannot be read: {exc}",
                fix="`ppy setup` to rewrite it, or `ppy config show` to see what is wrong",
            )
        )
        return
    tools = getattr(getattr(cfg, "claude", None), "allowed_tools", None)
    if tools is not None and str(tools).strip().upper() == "NONE":
        problems.append(
            Problem(
                code="claude_tools_empty",
                summary=(
                    "Claude workers have an empty tool profile, so every Claude dispatch is refused"
                ),
                fix="`ppy config claude --reset`",
            )
        )


def _repo_problems(problems: list[Problem]) -> None:
    from papaya_agent_runtime import memory, repos, solicit

    try:
        registered = repos.list_repos()
    except Exception:  # noqa: BLE001 - an unreadable state db is already reported elsewhere
        return
    if not registered:
        # Not blocking, and that is a correction rather than a relaxation. A ticket
        # brings its own repository: `papaya_events.ensure_repository` registers the
        # runtime-owned clone from the work item's repository URL the moment the
        # work is picked up. So an empty list means "nothing registered *ahead of
        # time*", which is the normal state of a machine somebody just connected —
        # and blocking on it made a runtime that could take work refuse to.
        problems.append(
            Problem(
                code="no_repos",
                summary="no repositories are registered ahead of time",
                fix=(
                    "a work item naming a repository registers it; "
                    "`ppy repo add` to register one ahead of time"
                ),
                owner=USER,
                blocking=False,
            )
        )
        return
    forgeless = sorted(r["name"] for r in registered if not r.get("forge_url"))
    if forgeless:
        problems.append(
            Problem(
                code="repo_without_forge",
                summary=(
                    f"no pull request can be opened for {', '.join(forgeless)}: "
                    "the repository has no forge recorded"
                ),
                fix="re-register it with `ppy repo add <path> --forge-url <url>`",
                owner=USER,
            )
        )
    unread = []
    for row in registered:
        notes = memory.repo_notes_path(row["name"])
        try:
            text = notes.read_text(encoding="utf-8") if notes.is_file() else ""
        except OSError:
            text = ""
        if solicit.NOTES_MARKER not in text:
            unread.append(row["name"])
    if unread:
        problems.append(
            Problem(
                code="repo_not_onboarded",
                summary=(
                    f"never read how {', '.join(sorted(unread))} builds, tests or gates, "
                    "so a first dispatch there would be guessing"
                ),
                fix="`ppy repo onboard <name>`",
                blocking=False,
            )
        )


#: The onboarding sections whose commands are a repository's gate.
_GATE_SECTIONS = ("## How it builds and verifies", "## What CI actually runs")

#: A gate program that cannot run without another one on the worker's allowlist.
_GATE_IMPLIES = {"pnpm": ("node",), "npm": ("node",), "npx": ("node",), "yarn": ("node",)}

_COMMAND_SPLIT = re.compile(r"&&|\|\||[;|]")
_INLINE_CODE = re.compile(r"`([^`]+)`")


def gate_commands(notes: str) -> list[str]:
    """The build, test and CI commands an onboarding wrote into a repository's notes."""
    from papaya_agent_runtime import solicit

    if solicit.NOTES_MARKER not in notes:
        return []
    block = notes.partition(solicit.NOTES_MARKER)[2].partition(solicit.NOTES_END)[0]
    commands: list[str] = []
    section = ""
    for line in block.splitlines():
        if line.startswith("## "):
            section = line.strip()
        elif section in _GATE_SECTIONS and line.startswith("- "):
            commands.extend(_INLINE_CODE.findall(line))
    return commands


def gate_programs(commands: list[str]) -> set[str]:
    """Every program those commands start, and the ones they cannot run without."""
    programs: set[str] = set()
    for command in commands:
        for segment in _COMMAND_SPLIT.split(command):
            words = [w for w in segment.split() if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w)]
            if words:
                programs.add(words[0])
                programs.update(_GATE_IMPLIES.get(words[0], ()))
    return programs


def _gate_tool_problems(problems: list[Problem]) -> None:
    """Does the stored Claude profile lack a tool a registered repository's gate runs?

    A worker whose allowlist refuses `node --test` spends its review round on a
    tool verb rather than the work (every JavaScript worker, 2026-09-16). Only a
    pattern the documented default carries is named, because the fix offered is
    restoring that default; a repository with no onboarding notes is skipped, since
    nobody knows its gate yet and `repo_not_onboarded` already says so.
    """
    from papaya_agent_runtime import memory, repos
    from papaya_agent_runtime.config import default_claude_allowed_tools, load_config
    from papaya_agent_runtime.paths import config_path

    if not config_path().exists():
        return
    try:
        stored = set(load_config().claude.allowed_tools)
        registered = repos.list_repos()
    except Exception:  # noqa: BLE001 - a bad config or state db is reported by the other checks
        return
    default = set(default_claude_allowed_tools())
    lacking: dict[str, list[str]] = {}
    for row in registered:
        try:
            notes = memory.repo_notes_path(row["name"]).read_text(encoding="utf-8")
        except OSError:
            continue
        for program in sorted(gate_programs(gate_commands(notes))):
            pattern = f"Bash({program}:*)"
            if pattern in default and pattern not in stored:
                lacking.setdefault(pattern, []).append(row["name"])
    if not lacking:
        return
    needed = "; ".join(
        f"{pattern} ({', '.join(sorted(names))})" for pattern, names in sorted(lacking.items())
    )
    problems.append(
        Problem(
            code="claude_tools_lack_gate",
            summary=(
                "Claude workers would be refused a tool a registered repository's gate runs: "
                f"{needed}"
            ),
            fix="`ppy config claude --reset`",
            owner=USER,
            blocking=False,
        )
    )


def _papaya_problems(problems: list[Problem]) -> None:
    """A missing workspace is never blocking — that is a standing rule, not a default.

    A runtime that refuses to build code because Papaya is unreachable is worse than
    one that builds code quietly, so this can only ever be a warning.
    """
    from papaya_agent_runtime import papaya

    if papaya.status()["state"] != "connected":
        problems.append(
            Problem(
                code="papaya_not_connected",
                summary="not connected to Papaya: no workspace, no work items, no shared memory",
                fix="`ppy papaya connect`, or connect from the Papaya desktop app",
                blocking=False,
            )
        )


def _client_problems(problems: list[Problem]) -> None:
    """Is the embedded Papaya client older than the one that launched this runtime?

    The host execs this checkout and passes its own client version across, so the
    two can disagree: the app updates its pinned client, the checkout does not, and
    the runtime quietly runs an older loop than the machine around it expects. This
    is a warning and only ever a warning — the client refuses to delegate on a
    protocol mismatch, not on a version, so being a release behind is drift worth
    naming and never a reason to stop taking work.
    """
    from papaya_agent_runtime import capabilities

    behind = capabilities.client_behind_host()
    if behind is None:
        return
    embedded, host = behind
    problems.append(
        Problem(
            code="client_behind_host",
            summary=(
                f"the embedded Papaya client is older than the one that launched this "
                f"runtime: {embedded} here, {host} on the host"
            ),
            fix="update this checkout and run `uv sync`",
            blocking=False,
        )
    )


def check() -> Readiness:
    """The verdict for this instance, from local state only."""
    problems: list[Problem] = []
    _config_problems(problems)
    _harness_problems(problems)
    _repo_problems(problems)
    _gate_tool_problems(problems)
    _papaya_problems(problems)
    _client_problems(problems)
    if any(p.blocking for p in problems):
        state = BLOCKED
    elif problems:
        state = DEGRADED
    else:
        state = READY
    return Readiness(
        state=state,
        problems=problems,
        checked_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def headline(readiness: Readiness) -> str:
    """One sentence, for a diagnostic line or a status reply."""
    if readiness.state == READY:
        return "ready to take work"
    if readiness.state == DEGRADED:
        names = ", ".join(p.code for p in readiness.warnings)
        return f"can take work, with gaps ({names})"
    first = readiness.blockers[0]
    extra = len(readiness.blockers) - 1
    tail = f" (and {extra} more)" if extra else ""
    return f"cannot take work: {first.summary}{tail}"


def report(readiness: Readiness, *, agent: str = "", where: str = "") -> str:
    """What the owner of this connection should be told, in their DM.

    Written for a person who may not be at the machine: it says what cannot happen,
    what closes it, and — the part that decides whether this message is useful or
    just a complaint — which items the runtime is about to handle by itself.
    """
    who = agent or "This agent"
    if readiness.state == READY:
        return f"{who} is set up and ready to take work{f' on {where}' if where else ''}."

    lines: list[str] = []
    if readiness.state == BLOCKED:
        lines.append(
            f"{who} is connected but **cannot take work yet**"
            f"{f' — its runtime is at {where}' if where else ''}."
        )
    else:
        lines.append(
            f"{who} can take work{f' at {where}' if where else ''}, but some things are missing."
        )
    lines.append("")

    mine = [p for p in readiness.problems if p.owner == RUNTIME]
    yours = [p for p in readiness.problems if p.owner == USER]

    if yours:
        lines.append("**Needs you:**")
        for problem in yours:
            mark = "" if problem.blocking else " (not blocking)"
            lines.append(f"- {problem.summary}{mark} — {problem.fix}")
        lines.append("")
    if mine:
        lines.append("**I'll handle these myself on my next turn:**")
        for problem in mine:
            lines.append(f"- {problem.summary}")
        lines.append("")

    if readiness.state == BLOCKED and not yours:
        lines.append(
            "Nothing here needs you — open a session on that machine and I'll sort it out."
        )
    return "\n".join(lines).rstrip()


# ── Saying it once, not on every wake ───────────────────────────────────────


def already_reported(conn: sqlite3.Connection, readiness: Readiness) -> bool:
    """Has this exact set of problems already been told to the owner?"""
    row = conn.execute(
        "SELECT fingerprint FROM readiness_reports WHERE fingerprint = ?",
        (readiness.fingerprint,),
    ).fetchone()
    return row is not None


def mark_reported(conn: sqlite3.Connection, readiness: Readiness) -> None:
    """Record that the owner has been told, so a quiet wake stays quiet.

    Keyed on the fingerprint rather than the clock: a problem that is still there
    tomorrow is not news, and a *different* problem is, however soon it appears.
    """
    conn.execute(
        """
        INSERT INTO readiness_reports (fingerprint, state, reported_at)
        VALUES (?, ?, ?)
        ON CONFLICT(fingerprint) DO UPDATE SET
            state = excluded.state, reported_at = excluded.reported_at
        """,
        (readiness.fingerprint, readiness.state, datetime.now(UTC).isoformat(timespec="seconds")),
    )
    conn.commit()


def forget_reports(conn: sqlite3.Connection) -> None:
    """Clear the record, so the next check reports whatever it finds."""
    conn.execute("DELETE FROM readiness_reports")
    conn.commit()


__all__ = [
    "BLOCKED",
    "DEGRADED",
    "READY",
    "RUNTIME",
    "USER",
    "Problem",
    "Readiness",
    "already_reported",
    "check",
    "forget_reports",
    "headline",
    "mark_reported",
    "report",
]
