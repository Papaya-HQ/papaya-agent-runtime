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

Most checks read local state only. The machine checks (`_machine_problems`) ask
the machine itself — `gh auth status`, `docker info`, the free disk — through one
seam, :data:`machine`, so the hermetic suite never asks the real one. The
fingerprint exists so the same problem is reported once rather than on every wake.

A problem that carries ``steps`` is a *blocker* in :mod:`papaya_agent_runtime.blockers`'
sense: something only a person can remedy, with the literal commands that do it.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

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
    #: For a problem only a person can remedy: a short title with nothing private
    #: in it, and the literal commands that remedy it, in order, ending with what
    #: the runtime does by itself afterwards. Empty for everything else.
    title: str = ""
    steps: tuple[str, ...] = ()
    #: What it is about, when that is narrower than the machine: a forge host.
    scope: str = ""
    #: The registered repositories it stops. Work on one of them is refused at
    #: pickup even though the verdict as a whole is not blocked.
    repos: tuple[str, ...] = ()
    #: A fact about how this runtime is running, not a gap: never blocking, never a
    #: blocker, never a deficiency, and it leaves a ready runtime ready.
    info: bool = False


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
        return [p for p in self.problems if not p.blocking and not p.info]

    @property
    def notes(self) -> list[Problem]:
        """The informational entries: how this runtime runs, not what it lacks."""
        return [p for p in self.problems if p.info]

    @property
    def fingerprint(self) -> str:
        """A stable id for *this set of problems*.

        Reporting is keyed on it, so an unchanged situation is said once and a
        changed one is said again. Codes only: the wording may improve without
        making the runtime repeat itself to the same person. Informational entries
        are not problems anybody is told about, so they are not in it.
        """
        codes = ",".join(sorted(p.code for p in self.problems if not p.info))
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
    steps: list[str] = []
    for provider in sorted(set(stranded.values())):
        steps.extend(harness_steps(report, provider))
    return Problem(
        code="provider_unusable",
        summary=(
            f"configured to use a harness this machine cannot launch: {roles}; "
            f"usable here: {', '.join(usable)}"
        ),
        fix="; ".join(fixes) + " — or `ppy config models` to choose a harness that works here",
        owner=USER,
        title="The coding harness this runtime is set to use cannot start on this machine",
        steps=(*steps, AFTER),
    )


#: How each harness is installed and signed in, as a person types it.
_HARNESS_INSTALL = {
    "claude": "npm install -g @anthropic-ai/claude-code",
    "codex": "npm install -g @openai/codex",
}
_HARNESS_LOGIN = {"claude": "claude auth login", "codex": "codex login"}

#: The last step of every blocker: what happens once the person has done theirs.
AFTER = (
    "Nothing to restart: the runtime checks again within a few minutes and starts "
    "taking work by itself."
)


def harness_steps(report: dict, provider: str) -> list[str]:
    """Install (when the binary is missing) and sign in to one harness."""
    tool = next((h for h in report.get("harnesses", []) if h.get("name") == provider), {})
    steps = [] if tool.get("path") else [_HARNESS_INSTALL.get(provider, f"install {provider}")]
    return [*steps, _HARNESS_LOGIN.get(provider, f"sign in to {provider}")]


def _connection_harness() -> str:
    """The harness this machine was connected with, or Claude when nobody said."""
    from papaya_agent_runtime import papaya

    try:
        who = papaya.identity()
    except Exception:  # noqa: BLE001 - an unreadable connection is reported by its own check
        who = None
    harness = str(getattr(who, "harness", "") or "")
    return "codex" if "codex" in harness else "claude"


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
                title="No coding harness is installed and signed in on this machine",
                steps=(*harness_steps(report, _connection_harness()), AFTER),
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
    from papaya_agent_runtime.config import effective_claude_tools

    if not effective_claude_tools(cfg):
        problems.append(
            Problem(
                code="claude_tools_empty",
                summary=(
                    "Claude workers have an empty tool profile, so every Claude dispatch is refused"
                ),
                fix="`ppy config claude --reset`",
                owner=USER,
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
    for row in sorted(registered, key=lambda r: str(r["name"])):
        name = str(row["name"])
        missing = [] if name in unread else solicit.gate_unknowns(row)
        if not missing:
            continue
        # The repository owns its gates. When its own instructions do not say which
        # command is the quick gate and which is the full suite, the runtime asks the
        # owner rather than guessing (a guessed `make verify` became every worker's
        # local gate on 2026-09-17).
        question = gate_question(name, missing)
        searched = (
            "nothing in its instructions (AGENTS.md, CLAUDE.md, CONTRIBUTING, testing docs), "
            "its pull-request CI, its Makefile or package scripts, or its push hooks says it"
        )
        problems.append(
            Problem(
                code="repo_without_gate_policy",
                summary=(
                    f"{name} does not say its {' or its '.join(missing)} ({searched}): {question}"
                ),
                fix=(
                    f"say it in {name}'s AGENTS.md (then `ppy repo onboard {name}`), or "
                    f'`ppy repo set {name} --local-gate "<quick gate>" '
                    '--full-suite-command "<full suite>"`'
                ),
                owner=USER,
                blocking=False,
                title="A repository does not say how its work is gated",
                steps=(
                    question,
                    f"write the answer into {name}'s AGENTS.md, e.g. \"run `<quick gate>` while "
                    'working, `<full suite>` before a PR", then: '
                    f"ppy repo onboard {name}",
                    f'or record it here only: ppy repo set {name} --local-gate "<quick gate>" '
                    '--full-suite-command "<full suite>"',
                    AFTER,
                ),
                scope=name,
            )
        )


def gate_question(name: str, missing: list[str]) -> str:
    """The exact question a repository's owner is asked about its unknown gates."""
    asks = []
    if "scoped gate" in missing:
        asks.append(
            f"which command is {name}'s quick gate (lint, type check and the touched tests, "
            "run before handing work back)"
        )
    if "full suite" in missing:
        asks.append(f"which command is {name}'s full suite (run once before a pull request)")
    return "; and ".join(asks)[:1].upper() + "; and ".join(asks)[1:] + "?"


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


def gate_tool_needs() -> dict[str, list[str]]:
    """Every `Bash(<program>:*)` a registered repository's onboarded gate runs, by repository.

    A repository with no onboarding notes is skipped, since nobody knows its gate yet
    and `repo_not_onboarded` already says so.
    """
    from papaya_agent_runtime import memory, repos

    try:
        registered = repos.list_repos()
    except Exception:  # noqa: BLE001 - an unreadable state db is reported by the other checks
        return {}
    needs: dict[str, list[str]] = {}
    for row in registered:
        try:
            notes = memory.repo_notes_path(row["name"]).read_text(encoding="utf-8")
        except OSError:
            continue
        for program in sorted(gate_programs(gate_commands(notes))):
            needs.setdefault(f"Bash({program}:*)", []).append(str(row["name"]))
    return {pattern: sorted(names) for pattern, names in needs.items()}


def _lock_fix(lock: str) -> str:
    section, _, key = lock.partition(".")
    return f"`ppy config {section} --unlock {key}` lets the runtime make it"


def _gate_tool_problems(problems: list[Problem]) -> None:
    """Do the effective Claude tools lack one a registered repository's gate runs?

    A worker whose allowlist refuses `node --test` spends its review round on a
    tool verb rather than the work (every JavaScript worker, 2026-09-16). Only a
    pattern the code's profile carries is named: with the profile applied from the
    code, that can only be a pattern somebody dropped. Restoring it is the runtime's
    own job (`config_changes.apply`, at `ppy serve` start and on every ensure), so
    this is said as the runtime's item — unless a lock refuses it, which is a
    person's.
    """
    from papaya_agent_runtime import config_changes
    from papaya_agent_runtime.config import load_config
    from papaya_agent_runtime.paths import config_path

    if not config_path().exists():
        return
    try:
        cfg = load_config()
        remedies = [r for r in config_changes.planned(cfg) if r.evidence.get("repositories")]
    except Exception:  # noqa: BLE001 - a bad config or state db is reported by the other checks
        return
    if not remedies:
        return
    needed = "; ".join(
        f"{r.pattern} ({', '.join(r.evidence['repositories'])})"
        for r in sorted(remedies, key=lambda r: r.pattern)
    )
    locks = sorted({lock for r in remedies if (lock := r.lock(cfg)) is not None})
    summary = (
        f"Claude workers would be refused a tool a registered repository's gate runs: {needed}"
    )
    if locks:
        problems.append(
            Problem(
                code="claude_tools_lack_gate",
                summary=f"{summary}; not restored because {', '.join(locks)} is locked",
                fix="; ".join(_lock_fix(lock) for lock in locks),
                owner=USER,
                blocking=False,
            )
        )
        return
    problems.append(
        Problem(
            code="claude_tools_lack_gate",
            summary=summary,
            fix="the runtime restores it when `ppy serve` starts or a repository is ensured",
            blocking=False,
        )
    )


def _gate_budget_problems(problems: list[Problem]) -> None:
    """Is a registered repository's gate, by its own history, longer than a tool call?

    That is exactly the repository where `ppy gate run` is the only way a gate
    finishes: run as a tool call it is cut off at ten minutes. Only a budget the
    repository's history derived (or a person set) says so; a default says nothing.
    """
    from papaya_agent_runtime import budgets, repos
    from papaya_agent_runtime.state import init_db

    try:
        registered = repos.list_repos()
        conn = init_db() if registered else None
    except Exception:  # noqa: BLE001 - an unreadable state db is reported elsewhere
        return
    over: list[str] = []
    try:
        for row in registered:
            for kind in (budgets.GATE, budgets.FULL_SUITE):
                found = budgets.budget(str(row["name"]), kind, conn=conn)
                if found.source == budgets.DEFAULT or found.seconds <= budgets.TOOL_CAP_SECONDS:
                    continue
                label = "local gate" if kind == budgets.GATE else "full suite"
                over.append(f"{row['name']} {label} {int(found.seconds // 60)}m ({found.source})")
    finally:
        if conn is not None:
            conn.close()
    if not over:
        return
    problems.append(
        Problem(
            code="gate_budget_over_tool_cap",
            summary=(
                "gate budgets longer than the harness's ten-minute tool cap: " + "; ".join(over)
            ),
            fix=(
                "run those gates only with `ppy gate run` (the environment block and briefs "
                "say so); `ppy repo budgets <name>` shows the derivation"
            ),
            blocking=False,
        )
    )


GATE_ENV_NOT_ISOLATED = "gate_env_not_isolated"


def _gate_isolation_problems(problems: list[Problem]) -> None:
    """Could two gates on a registered repository run against one database?

    Issue #47 (2026-09-17): every backend gate and a review's baseline check ran
    against the repository's default test database at once, because the repository
    ships a compose file but had no private stack declared, so each task's gate
    environment had nothing private in it. A compose repository is isolated per task
    only with a port base and database URL templates that carry ``{task_id}``.
    """
    from papaya_agent_runtime import environment, repos

    try:
        registered = repos.list_repos()
    except Exception:  # noqa: BLE001 - an unreadable state db is reported elsewhere
        return
    gaps: list[str] = []
    for row in registered:
        path = str(row.get("local_path") or "")
        has_compose_file = bool(path) and any(
            machine.is_file(f"{path}/{name}") for name in _COMPOSE_FILES
        )
        found = environment.isolation_gaps(
            environment.for_repo(row), has_compose_file=has_compose_file
        )
        if found:
            gaps.append(f"{row['name']} ({'; '.join(found)})")
    if not gaps:
        return
    problems.append(
        Problem(
            code=GATE_ENV_NOT_ISOLATED,
            summary=(
                "gates on these repositories can share one database, so one gate's run "
                "can fail another's: " + ", ".join(gaps)
            ),
            fix=(
                "`ppy repo set <name> --compose-stack yes --db-port-base <port> "
                "--db-port-variable <the Makefile's port variable> "
                '--test-db-url-template "postgresql://...:{port}/<db>_{task_id}"`'
            ),
            owner=USER,
            blocking=False,
        )
    )


def _learned_tool_problems(problems: list[Problem]) -> None:
    """Denied tools: a learned one a lock refused, and a worker's request awaiting a person."""
    from papaya_agent_runtime import capability_requests, config_changes
    from papaya_agent_runtime.config import load_config
    from papaya_agent_runtime.paths import config_path

    # A plain command refused outside the safe family is a capability request now, and a
    # pending one is a person's decision with its own commands.
    problems.extend(capability_requests.problems())
    if not config_path().exists():
        return
    try:
        cfg = load_config()
        locked = [(r, lock) for r, lock in config_changes.blocked(cfg) if r.evidence.get("command")]
    except Exception:  # noqa: BLE001 - a bad config or state db is reported by the other checks
        return
    if locked:
        problems.append(
            Problem(
                code="config_locked",
                summary=(
                    "the runtime would have changed a locked key: "
                    + "; ".join(
                        f"{r.pattern} into {r.key} after a denied `{r.evidence['command']}` "
                        f"({lock} is locked)"
                        for r, lock in locked
                    )
                ),
                fix="; ".join(sorted({_lock_fix(lock) for _, lock in locked})),
                owner=USER,
                blocking=False,
            )
        )


PAPAYA_NOT_CONNECTED = "papaya_not_connected"
#: The connected agent is shared, so Papaya memory is not this runtime's to write.
MEMORY_UNAVAILABLE_SHARED_AGENT = "memory_unavailable_shared_agent"


def _papaya_problems(problems: list[Problem]) -> None:
    """No connection is a mode, not a gap — that is a standing rule, not a default.

    Everything local works without Papaya, so a runtime running standalone is as
    ready as a connected one. The entry is informational: it has no steps (it is
    not a blocker), it is not blocking, and it does not make a verdict degraded.

    So is a shared agent's missing Papaya memory: the runtime keeps what it learns in
    repository notes instead, and nothing about it is a gap anyone should close. What
    kind of agent this is comes from the last read of Papaya's agent record
    (`papaya.known_agent_kind`), never from the network here.
    """
    from papaya_agent_runtime import papaya

    who = papaya.identity()
    kind = papaya.known_agent_kind(who.agent_id) if who is not None else None
    if kind is not None and kind.memory == papaya.MEMORY_REPO_NOTES_ONLY:
        problems.append(
            Problem(
                code=MEMORY_UNAVAILABLE_SHARED_AGENT,
                summary=(
                    "connected as a shared agent: Papaya keeps no memories this runtime "
                    "proposes, so turns keep durable facts in the repositories' memory notes"
                ),
                fix="nothing to fix; connect as a personal agent if Papaya memory is wanted",
                owner=USER,
                blocking=False,
                info=True,
            )
        )
    if papaya.status()["state"] != "connected":
        problems.append(
            Problem(
                code=PAPAYA_NOT_CONNECTED,
                summary=(
                    "running without Papaya: work is local only, tickets and comments do "
                    "not flow in or out"
                ),
                fix="`ppy papaya connect`, or connect from the Papaya desktop app, when wanted",
                owner=USER,
                blocking=False,
                info=True,
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


ENVIRONMENT_BROKEN = "environment_broken"


def environment_path() -> Path:
    """This checkout's environment, where `bin/ppy` runs everything from."""
    import os

    import papaya_agent_runtime

    configured = os.environ.get("UV_PROJECT_ENVIRONMENT")
    if configured:
        return Path(configured)
    return Path(papaya_agent_runtime.__file__).resolve().parents[2] / ".venv"


def environment_imports(env: Path) -> bool:
    """Would the runtime's dependency import from ``env``? Read from disk, not by running it.

    Its interpreter must resolve (a swapped or deleted build leaves a dangling link)
    and the Papaya client must be in its site-packages — the package a refused or
    interrupted sync left missing on 2026-09-16.
    """
    if not (env / "bin" / "python").exists():
        return False
    return any(
        (site / "papaya_agent_client" / "__init__.py").is_file()
        for site in env.glob("lib/python*/site-packages")
    )


def _environment_problems(problems: list[Problem]) -> None:
    env = environment_path()
    if environment_imports(env):
        return
    problems.append(
        Problem(
            code=ENVIRONMENT_BROKEN,
            summary=(
                "this checkout's environment does not import the runtime's packages "
                "(the Papaya client is missing from it)"
            ),
            fix=(
                "the launcher rebuilds it before anything else on the next `ppy serve` "
                "start; `ppy env sync` rebuilds it now"
            ),
        )
    )


def _start_failure_problems(problems: list[Problem]) -> None:
    """The sentence a `ppy serve` that could not start left, until a start has said it.

    A start that fails cannot tell anybody but its own stderr. The next one that
    does start reports it once, with its steps, as a blocker (not blocking: that
    start is running), and clears it; the ledger then says once that it cleared.
    """
    from papaya_agent_runtime import takeover
    from papaya_agent_runtime.paths import ppy_home

    record = takeover.start_failure(str(ppy_home().resolve()))
    if record is None:
        return
    line = str(record.get("line") or "")
    problems.append(
        Problem(
            code=takeover.START_FAILURE_CODE,
            summary=f"`ppy serve` could not start at {record.get('at') or 'an earlier start'}: "
            + line,
            fix="nothing now: a later start succeeded",
            owner=USER,
            blocking=False,
            title=takeover.title_for(line),
            steps=tuple(str(step) for step in record.get("steps") or ()),
        )
    )


# ── the machine: the forge, the toolchains, Docker, the disk ────────────────

FORGE_UNAUTHENTICATED = "forge_unauthenticated"
GH_MISSING = "gh_missing"
REPO_UNREACHABLE = "repo_unreachable"
#: A base clone fetches from a local checkout, not its forge, and the start could not fix it.
REPO_ORIGIN_IS_LOCAL = "repo_origin_is_local"
NODE_MISSING = "node_missing"
UV_MISSING = "uv_missing"
DOCKER_NOT_RUNNING = "docker_not_running"
DISK_LOW = "disk_low"

#: The blockers that make a pull request impossible, so delivery is refused too.
DELIVERY_CODES = frozenset({FORGE_UNAUTHENTICATED, GH_MISSING, REPO_UNREACHABLE})

#: Below this much free space a worktree, a dependency install or a Docker image
#: fails part-way, which is worse than not starting.
DISK_FLOOR_BYTES = 5 * 1024**3

_COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")
_NODE_PROGRAMS = frozenset({"node", "pnpm", "npm", "npx", "yarn"})


class Machine:
    """What readiness asks the machine, behind one seam.

    The hermetic suite replaces :data:`machine` with a healthy fake
    (``tests/conftest.py``), so no test runs `gh auth status` against a real
    account or reads a developer's disk; a test that needs a broken machine
    replaces it again.
    """

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def run(
        self, argv: list[str], timeout: float = 20.0, input: str | None = None
    ) -> tuple[int, str]:
        """Exit status and combined output; 127 when the program cannot be run."""
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout, check=False, input=input
            )
        except (OSError, subprocess.TimeoutExpired):
            return 127, ""
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def free_bytes(self, path: str) -> int | None:
        try:
            return shutil.disk_usage(path).free
        except OSError:
            return None

    def is_file(self, path: str) -> bool:
        try:
            return Path(path).is_file()
        except OSError:
            return False

    @property
    def platform(self) -> str:
        return sys.platform


machine = Machine()

_HOST_PATTERNS = (
    re.compile(r"^(?:https?|ssh|git)://(?:[^@/]+@)?(?P<host>[^/:]+)(?::\d+)?/"),
    re.compile(r"^[^/@\s]+@(?P<host>[^/:\s]+):"),
)


def forge_host(url: str | None) -> str | None:
    """The host of a remote URL (https, ssh or scp-style), or None for a path."""
    for pattern in _HOST_PATTERNS:
        match = pattern.match((url or "").strip())
        if match:
            return match.group("host").lower()
    return None


def _is_github(host: str) -> bool:
    """`gh` speaks to GitHub and GitHub Enterprise only; other forges are not asked."""
    return host == "github.com" or "github" in host


def _install(tool: str) -> list[str]:
    darwin = machine.platform == "darwin"
    return {
        "gh": ["brew install gh"] if darwin else ["see https://cli.github.com to install gh"],
        "node": ["brew install node"] if darwin else ["see https://nodejs.org to install Node"],
        "uv": ["curl -LsSf https://astral.sh/uv/install.sh | sh"],
        "docker": ["brew install --cask docker"]
        if darwin
        else ["see https://docs.docker.com/engine/install/ to install Docker"],
    }[tool]


def _login_steps(host: str) -> list[str]:
    return [
        f"gh auth login --hostname {host} --git-protocol https --web",
        "gh auth setup-git",
        f"gh auth status --hostname {host}",
    ]


def _repo_forge(row: dict) -> str | None:
    return row.get("forge_url") or row.get("origin")


def _forge_problems(problems: list[Problem], registered: list[dict]) -> None:
    """Can `gh` act on every GitHub host this runtime delivers to?

    Delivery opens a pull request with `gh`; with nobody signed in, the first
    delivery fails with a gh error nobody sees. The runtime's own origin counts
    too: it is how this checkout is updated.
    """
    from papaya_agent_runtime import repos
    from papaya_agent_runtime.manager.launch import repo_root

    by_host: dict[str, list[dict]] = {}
    for row in registered:
        host = forge_host(_repo_forge(row))
        if host and _is_github(host):
            by_host.setdefault(host, []).append(row)
    code, out = machine.run(["git", "-C", str(repo_root()), "remote", "get-url", "origin"])
    own = forge_host(out.strip()) if code == 0 else None
    if own and _is_github(own):
        by_host.setdefault(own, [])
    if not by_host:
        return

    def names(rows: list[dict]) -> tuple[str, ...]:
        return tuple(sorted(str(r["name"]) for r in rows))

    if machine.which("gh") is None:
        every = [row for rows in by_host.values() for row in rows]
        first = sorted(by_host)[0]
        problems.append(
            Problem(
                code=GH_MISSING,
                summary="the GitHub CLI (`gh`) is not installed, so no pull request can be opened",
                fix="install `gh`, then `gh auth login`",
                owner=USER,
                blocking=False,
                title="The GitHub CLI is not installed on this machine",
                steps=(*_install("gh"), *_login_steps(first), AFTER),
                repos=names(every),
            )
        )
        return

    for host in sorted(by_host):
        status, _ = machine.run(["gh", "auth", "status", "--hostname", host])
        if status != 0:
            label = "GitHub" if host == "github.com" else f"GitHub ({host})"
            problems.append(
                Problem(
                    code=FORGE_UNAUTHENTICATED,
                    summary=(f"`gh` is not signed in to {host}, so work there cannot be delivered"),
                    fix=f"`gh auth login --hostname {host}`",
                    owner=USER,
                    blocking=False,
                    title=f"{label} is not signed in on this machine",
                    steps=(*_login_steps(host), AFTER),
                    scope=host,
                    repos=names(by_host[host]),
                )
            )
            continue
        unreachable = []
        for row in by_host[host]:
            slug = repos.forge_slug(_repo_forge(row))
            if not slug:
                continue
            rc, answer = machine.run(
                ["gh", "api", f"repos/{slug}", "--hostname", host, "--jq", ".permissions.push"]
            )
            if rc != 0 or answer.strip() == "false":
                unreachable.append(row)
        if unreachable:
            problems.append(
                Problem(
                    code=REPO_UNREACHABLE,
                    summary=(
                        f"the account `gh` is signed in to on {host} cannot read or push "
                        f"{', '.join(names(unreachable))}"
                    ),
                    fix="ask for write access, or sign in as an account that has it",
                    owner=USER,
                    blocking=False,
                    title="A repository this machine works on cannot be read or pushed "
                    "with the GitHub account signed in here",
                    steps=(
                        f"gh auth status --hostname {host}",
                        "ask the repository's owner to give <your-github-username> write access",
                        f"or: gh auth login --hostname {host} --git-protocol https --web",
                        AFTER,
                    ),
                    scope=host,
                    repos=names(unreachable),
                )
            )


def _origin_problems(problems: list[Problem], registered: list[dict]) -> None:
    """A base clone whose `origin` is a local checkout rather than its forge.

    `ppy serve`'s start and `ppy repo sync` rewrite such an `origin` whenever the
    forge answers, so one still standing means the forge could not be reached. Work
    there would branch from whatever the checkout had fetched — on 2026-09-16, a
    person's feature branch — so the repository is refused until it is fixed.
    """
    from papaya_agent_runtime import repos

    stranded: list[tuple[str, str, str, str]] = []
    for row in registered:
        forge, path = row.get("forge_url"), str(row.get("local_path") or "")
        if not forge or not path:
            continue
        code, out = machine.run(["git", "-C", path, "config", "--get", "remote.origin.url"])
        origin = out.strip() if code == 0 else ""
        if origin and repos.is_local_remote(origin) and origin.rstrip("/") != forge.rstrip("/"):
            stranded.append((str(row["name"]), path, origin, forge))
    if not stranded:
        return
    name, path, origin, forge = stranded[0]
    names = tuple(sorted(s[0] for s in stranded))
    problems.append(
        Problem(
            code=REPO_ORIGIN_IS_LOCAL,
            summary=(
                f"{', '.join(names)} fetch from a local checkout instead of the forge "
                f"({name}: {origin}), and the forge could not be reached to fix it"
            ),
            fix=f"make the forge reachable, then `ppy repo sync {name}`",
            owner=USER,
            blocking=False,
            title="A repository's base clone fetches from a local checkout, not its forge",
            steps=(
                f"git ls-remote --symref {forge} HEAD",
                "if that fails: gh auth login --hostname github.com --git-protocol https --web",
                f"ppy repo sync {name}",
                AFTER,
            ),
            repos=names,
        )
    )


def _toolchain_problems(problems: list[Problem], registered: list[dict]) -> None:
    """Node, uv and Docker, for the repositories that need them."""
    from papaya_agent_runtime import memory

    needs: dict[str, list[str]] = {"node": [], "uv": [], "docker": []}
    for row in registered:
        name, path = str(row["name"]), str(row.get("local_path") or "")
        try:
            notes = memory.repo_notes_path(name).read_text(encoding="utf-8")
        except OSError:
            notes = ""
        programs = gate_programs(gate_commands(notes))
        if programs & _NODE_PROGRAMS or (path and machine.is_file(f"{path}/package.json")):
            needs["node"].append(name)
        if "uv" in programs or (path and machine.is_file(f"{path}/uv.lock")):
            needs["uv"].append(name)
        if path and any(machine.is_file(f"{path}/{f}") for f in _COMPOSE_FILES):
            needs["docker"].append(name)

    for tool, code, title in (
        ("node", NODE_MISSING, "Node is not installed, and a repository here needs it"),
        ("uv", UV_MISSING, "uv is not installed, and a repository here needs it"),
    ):
        if needs[tool] and machine.which(tool) is None:
            problems.append(
                Problem(
                    code=code,
                    summary=f"`{tool}` is not installed; {', '.join(needs[tool])} need it",
                    fix=f"install {tool}",
                    owner=USER,
                    blocking=False,
                    title=title,
                    steps=(*_install(tool), f"{tool} --version", AFTER),
                    repos=tuple(sorted(needs[tool])),
                )
            )

    if not needs["docker"]:
        return
    installed = machine.which("docker") is not None
    if installed and machine.run(["docker", "info"])[0] == 0:
        return
    start = "open -a Docker" if machine.platform == "darwin" else "sudo systemctl start docker"
    problems.append(
        Problem(
            code=DOCKER_NOT_RUNNING,
            summary=(
                f"Docker is not running; {', '.join(sorted(needs['docker']))} "
                "run their services with compose"
            ),
            fix="start Docker",
            owner=USER,
            blocking=False,
            title="Docker is not running, and a repository here runs its services in it",
            steps=(*([] if installed else _install("docker")), start, "docker info", AFTER),
            repos=tuple(sorted(needs["docker"])),
        )
    )


def _disk_problems(problems: list[Problem]) -> None:
    from papaya_agent_runtime.paths import ppy_home

    home = ppy_home()
    free = machine.free_bytes(str(home if home.exists() else Path.cwd()))
    if free is None or free >= DISK_FLOOR_BYTES:
        return
    problems.append(
        Problem(
            code=DISK_LOW,
            summary=(
                f"{free / 1024**3:.1f} GiB free on this disk, below the "
                f"{DISK_FLOOR_BYTES // 1024**3} GiB a worker needs"
            ),
            fix="free some disk space",
            owner=USER,
            title="This machine is almost out of disk space",
            steps=("ppy worktree prune", "df -h ~", "free up at least 5 GiB", AFTER),
        )
    )


def _machine_problems(problems: list[Problem]) -> None:
    from papaya_agent_runtime import repos

    try:
        registered = repos.list_repos()
    except Exception:  # noqa: BLE001 - an unreadable state db is already reported elsewhere
        registered = []
    for probe in (
        lambda: _forge_problems(problems, registered),
        lambda: _origin_problems(problems, registered),
        lambda: _toolchain_problems(problems, registered),
        lambda: _disk_problems(problems),
    ):
        try:
            probe()
        except Exception:  # noqa: BLE001 - a readiness check must never be the thing that breaks
            continue


def setup_blocker(
    verdict: Readiness, repo: str | None = None, *, delivery: bool = False
) -> Problem | None:
    """The blocker that stops this work here, if one does.

    At pickup (``delivery=False``): any blocking problem a person must remedy, or
    one scoped to ``repo``. At delivery: only what makes a pull request impossible
    for ``repo``. ``None`` means nothing a person has to do stands in the way —
    which is not the same as ready, since the runtime's own gaps are not blockers.
    """
    for problem in verdict.problems:
        if not problem.steps:
            continue
        if delivery:
            if repo and repo in problem.repos and problem.code in DELIVERY_CODES:
                return problem
            continue
        if problem.blocking or (repo and repo in problem.repos):
            return problem
    return None


def _owed_problems(problems: list[Problem]) -> None:
    """A worker waiting on the manager past the grace, with no live ticket covering it.

    `ppy serve` works a held ticket's workers itself. Anything else (a worker
    dispatched by hand, a ticket that ended) is the interactive manager's, and when no
    manager takes it up within `owed.GRACE_SECONDS` it is a person's: said here, so the
    blocker watch tells them whichever way this runtime is running. One problem per
    task and status, so a change of status is news again.
    """
    from papaya_agent_runtime import owed
    from papaya_agent_runtime.paths import db_path
    from papaya_agent_runtime.state import init_db

    if not db_path().exists():
        return
    try:
        conn = init_db()
        try:
            items = owed.overdue(owed.collect(conn))
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - a verdict never fails on its own evidence
        return
    for item in items:
        problems.append(
            Problem(
                code=owed.PROBLEM_CODE,
                summary=item.line(),
                fix=item.next_step,
                owner=USER,
                blocking=False,
                title=f"Worker task {item.task_id} is waiting on its manager",
                steps=(
                    f"worker task {item.task_id}: {item.reason}",
                    f"take it up from a manager session in the runtime directory: {item.next_step}",
                    "the runtime clears this once the task moves on",
                ),
                scope=f"task:{item.task_id}:{item.status}",
            )
        )


def check() -> Readiness:
    """The verdict for this instance."""
    problems: list[Problem] = []
    _environment_problems(problems)
    _start_failure_problems(problems)
    _config_problems(problems)
    _harness_problems(problems)
    _repo_problems(problems)
    _gate_tool_problems(problems)
    _learned_tool_problems(problems)
    _gate_budget_problems(problems)
    _gate_isolation_problems(problems)
    _papaya_problems(problems)
    _client_problems(problems)
    _machine_problems(problems)
    _owed_problems(problems)
    if any(p.blocking for p in problems):
        state = BLOCKED
    elif any(not p.info for p in problems):
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

    mine = [p for p in readiness.problems if p.owner == RUNTIME and not p.info]
    yours = [p for p in readiness.problems if p.owner == USER and not p.info]

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
    "AFTER",
    "BLOCKED",
    "DEGRADED",
    "DELIVERY_CODES",
    "DISK_LOW",
    "DOCKER_NOT_RUNNING",
    "FORGE_UNAUTHENTICATED",
    "GH_MISSING",
    "NODE_MISSING",
    "READY",
    "REPO_UNREACHABLE",
    "RUNTIME",
    "USER",
    "UV_MISSING",
    "Machine",
    "Problem",
    "Readiness",
    "already_reported",
    "check",
    "forge_host",
    "forget_reports",
    "harness_steps",
    "headline",
    "machine",
    "mark_reported",
    "report",
    "setup_blocker",
]
