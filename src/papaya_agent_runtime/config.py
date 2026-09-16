"""Configuration model for Papaya Agent Runtime (``.ppy/config.toml``).

Holds the manager profile, the hard worker ceiling, cost posture, and standing
repository authority. The runtime enforces these; prompts never do. Validation
is fail-closed: an unusable config raises rather than silently degrading.

The control plane keeps zero runtime package dependencies, so TOML is read with
the stdlib ``tomllib`` and written with a small purpose-built serializer for the
known, flat-ish schema.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from papaya_agent_runtime.paths import config_path

REASONING_LEVELS = ("low", "medium", "high", "xhigh")
COST_POSTURES = ("lean", "balanced", "quality")
PROVIDERS = ("claude", "codex")


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


@dataclass
class ManagerProfile:
    provider: str = "claude"
    model: str = "opus"
    reasoning: str = "high"


@dataclass
class WorkerCeiling:
    # Deliberately the same literal as ``ManagerProfile.provider``: the runtime does
    # not mix harnesses unless a person asked it to. This used to default to "codex"
    # while the manager defaulted to "claude", so a config that named no worker
    # provider silently produced a Claude manager driving Codex workers. An absent
    # ``worker.provider`` now resolves to the manager's (see ``_from_dict``); this
    # default only applies when there is no manager table either.
    provider: str = "claude"
    max_model: str = "gpt-5-codex"
    max_reasoning: str = "medium"
    # Defaults are independent of the ceiling: omitted task choices must resolve
    # deterministically instead of leaking through to a harness/account default.
    # Appended for positional-constructor compatibility with the original three
    # ceiling fields.
    default_model: str | None = None
    default_reasoning: str | None = None
    max_concurrent: int = 2
    # Capacity admitted only to fix a delivered pull request (red CI, conflicts, a branch
    # behind its base, reviewer comments): never a ticket's first dispatch, and never
    # taken out of `max_concurrent`, so PR fixes and new tickets never wait on each other.
    reconcile_slots: int = 1


@dataclass
class DeliveryPolicy:
    """What happens to a delivered pull request while nobody merges it."""

    # Hours a pull request may sit green, mergeable and unrequested before the ticket
    # says so once (or, on a repo with `auto_merge`, the runtime merges it).
    merge_after_hours: int = 24


@dataclass
class Authority:
    clone_fetch: bool = True
    create_worktrees: bool = True
    commit: bool = True
    push_branches: bool = True
    open_pr: bool = True
    merge: bool = False  # merge stays off by default


@dataclass
class ToolsPolicy:
    location: str = ".ppy/tools"
    update: str = "explicit"  # explicit | manual


@dataclass
class AssessmentPolicy:
    """Cadence and bounds for proactive manager performance reviews."""

    enabled: bool = True
    completed_runs: int = 5
    max_days: int = 14
    cooldown_days: int = 7
    minimum_runs: int = 2
    failure_trigger_count: int = 2
    max_actions: int = 3


#: This checkout's own launcher, wherever the checkout lives. Until 2026-09-16 the
#: profile carried ``Bash(/abs/path/to/checkout/bin/ppy:*)``, which made every stored
#: copy of it machine-specific.
PPY_LAUNCHER_PATTERN = "Bash(*/bin/ppy:*)"

_ABSOLUTE_LAUNCHER = re.compile(r"^Bash\(/.*/bin/ppy:\*\)$")

#: The documented Claude worker tool profile. It lives here and only here: a config
#: file records what a person added to it or took out of it, never a copy of it.
#:
#: Claude Code only gives a worker a shell when it is launched with
#: ``--allowedTools``. That list used to come *only* from
#: ``PPY_CLAUDE_ALLOWED_TOOLS`` in the supervisor's own environment and was
#: persisted nowhere, so every ``ppy supervisor serve`` had to export it first and a
#: dispatch after a restart silently produced a worker with no shell (2026-09-02).
#:
#: The first entries are the profile the Claude Code permission classifier accepted
#: on 2026-09-02, proved end to end by smoke task 77: progress reporting, uv, a
#: commit, and a push. A broader list including ``rm``, ``gh`` and ``export`` was
#: classifier-blocked, so it is deliberately absent — and workers never open PRs or
#: hold forge credentials anyway.
#:
#: The JavaScript toolchain, the file verbs and the read-only text tools arrived on
#: 2026-09-16, when every worker dispatched into a JavaScript repository reported
#: ``node --test`` denied and one could not ``cp`` its evidence into place: the list
#: had been copied from a Python-only manager. Containment is the write boundary,
#: not this verb list — a worker that may ``cp`` inside its worktree is still refused
#: outside it.
CLAUDE_PROFILE: tuple[str, ...] = (
    "Read",
    "Edit",
    "Write",
    "Glob",
    "Grep",
    "Bash(cd:*)",
    "Bash(git:*)",
    "Bash(uv:*)",
    "Bash(make:*)",
    "Bash(pytest:*)",
    "Bash(python:*)",
    "Bash(python3:*)",
    "Bash(ruff:*)",
    PPY_LAUNCHER_PATTERN,
    "Bash(./bin/ppy:*)",
    "Bash(ppy:*)",
    "Bash(ls:*)",
    "Bash(cat:*)",
    "Bash(mkdir:*)",
    "Bash(jq:*)",
    "Bash(pnpm:*)",
    "Bash(node:*)",
    "Bash(npm:*)",
    "Bash(npx:*)",
    "Bash(corepack:*)",
    "Bash(cp:*)",
    "Bash(mv:*)",
    "Bash(tee:*)",
    "Bash(touch:*)",
    "Bash(head:*)",
    "Bash(tail:*)",
    "Bash(wc:*)",
    "Bash(sed:*)",
    "Bash(find:*)",
    "Bash(sqlite3:*)",
)

#: Every profile a release has ever written into somebody's ``config.toml``, oldest
#: first, with the absolute launcher path already folded into
#: :data:`PPY_LAUNCHER_PATTERN`. Migration recognises a stored list by these, so they
#: are history: never edit one, append the next.
HISTORICAL_CLAUDE_PROFILES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "0.x profile of 2026-09-02",
        (
            "Read",
            "Edit",
            "Write",
            "Glob",
            "Grep",
            "Bash(cd:*)",
            "Bash(git:*)",
            "Bash(uv:*)",
            "Bash(make:*)",
            "Bash(pytest:*)",
            "Bash(python:*)",
            "Bash(ruff:*)",
            PPY_LAUNCHER_PATTERN,
            "Bash(./bin/ppy:*)",
            "Bash(ppy:*)",
            "Bash(ls:*)",
            "Bash(cat:*)",
            "Bash(mkdir:*)",
            "Bash(jq:*)",
            "Bash(pnpm:*)",
        ),
    ),
    (
        "0.x profile of 2026-09-16 (PR 19)",
        (
            "Read",
            "Edit",
            "Write",
            "Glob",
            "Grep",
            "Bash(cd:*)",
            "Bash(git:*)",
            "Bash(uv:*)",
            "Bash(make:*)",
            "Bash(pytest:*)",
            "Bash(python:*)",
            "Bash(python3:*)",
            "Bash(ruff:*)",
            PPY_LAUNCHER_PATTERN,
            "Bash(./bin/ppy:*)",
            "Bash(ppy:*)",
            "Bash(ls:*)",
            "Bash(cat:*)",
            "Bash(mkdir:*)",
            "Bash(jq:*)",
            "Bash(pnpm:*)",
            "Bash(node:*)",
            "Bash(npm:*)",
            "Bash(npx:*)",
            "Bash(corepack:*)",
            "Bash(cp:*)",
            "Bash(mv:*)",
            "Bash(tee:*)",
            "Bash(touch:*)",
            "Bash(head:*)",
            "Bash(tail:*)",
            "Bash(wc:*)",
            "Bash(sed:*)",
            "Bash(find:*)",
            "Bash(sqlite3:*)",
        ),
    ),
)

#: The config file format. 1 (implicit: no key) wrote every default, including a
#: verbatim copy of the Claude tool profile; 2 writes only what differs from the code.
CONFIG_VERSION = 2

#: The per-provider model a role gets when the file names none.
DEFAULT_MODELS = {"claude": "opus", "codex": "gpt-5-codex"}


def default_claude_allowed_tools() -> list[str]:
    """The code's Claude worker tool profile (:data:`CLAUDE_PROFILE`)."""
    return list(CLAUDE_PROFILE)


def normalise_tool(pattern: str) -> str:
    """One tool pattern, with a machine-specific launcher path made portable."""
    pattern = pattern.strip()
    return PPY_LAUNCHER_PATTERN if _ABSOLUTE_LAUNCHER.match(pattern) else pattern


@dataclass
class ClaudeProfile:
    """Settings that shape a Claude worker's session.

    The tools a worker launches with are :data:`CLAUDE_PROFILE` plus
    ``extra_tools`` minus ``dropped_tools`` (:func:`effective_claude_tools`), so a
    new release's profile applies on the next load with nothing to edit.
    ``locked`` names keys of this table the runtime must never change by itself.
    ``allowed_tools`` is the pre-delta verbatim list; it only survives loading when
    a person locked it.
    """

    extra_tools: list[str] = field(default_factory=list)
    dropped_tools: list[str] = field(default_factory=list)
    locked: list[str] = field(default_factory=list)
    allowed_tools: list[str] | None = None


@dataclass
class HealthPolicy:
    """How long a worker may stay silent before it is flagged as possibly stuck."""

    quiet_minutes: int = 15
    # Minutes a worker may run before it is flagged for never having posted a plan.
    plan_minutes: int = 10
    # Finished task stacks still consume Docker networks and database ports.
    max_stale_stacks: int = 4
    # Minutes a worker runs before `ppy serve`'s rounds check it is still on course.
    checkin_after: int = 20
    # Minutes a worker may run with nothing new on its remote branch before the rounds
    # check in to have it commit what is green and push; again every as many minutes.
    push_by_minutes: int = 45
    # Seconds between `ppy serve`'s rounds, when neither the flag nor the env says.
    rounds_interval: int = 300
    # Minutes, at most, between `ppy serve`'s liveness lines for a held ticket whose
    # worker or gate is active: the client's stall clock hears the work that way.
    liveness_minutes: int = 5


@dataclass
class UsagePolicy:
    """Advisory input-token ceilings; these report, they never stop work."""

    input_ceiling_per_task: int = 12_000_000
    input_ceiling_per_review: int = 3_000_000


@dataclass
class SelfReportPolicy:
    """Whether `ppy serve` opens GitHub issues about the runtime's own deficiencies."""

    enabled: bool = True
    # `owner/name` or a GitHub URL; empty means the origin of the running checkout.
    repo: str = ""
    # New issues a day; the rest wait in the ledger and open on later days.
    max_per_day: int = 5


@dataclass
class SupervisorPolicy:
    """How the supervisor `ppy serve` owns is stopped."""

    # Seconds a stopping supervisor waits for its workers to be recorded stopped
    # (their sessions stay resumable) before it exits anyway.
    stop_timeout: int = 30


@dataclass
class ForgePolicy:
    """How this runtime reaches the forge on a person's behalf."""

    # The client id of Papaya's GitHub OAuth app. When set, a machine whose `gh` is
    # not signed in is signed in through GitHub's device flow: the owner is sent a
    # code to enter, and the token goes straight into gh's own store. Not a secret.
    # Empty means the owner is sent the manual `gh auth login` steps instead.
    github_oauth_client_id: str = ""


@dataclass
class MMConfig:
    manager: ManagerProfile = field(default_factory=ManagerProfile)
    worker: WorkerCeiling = field(default_factory=WorkerCeiling)
    cost_posture: str = "lean"
    authority: Authority = field(default_factory=Authority)
    tools: ToolsPolicy = field(default_factory=ToolsPolicy)
    assessments: AssessmentPolicy = field(default_factory=AssessmentPolicy)
    health: HealthPolicy = field(default_factory=HealthPolicy)
    usage: UsagePolicy = field(default_factory=UsagePolicy)
    claude: ClaudeProfile = field(default_factory=ClaudeProfile)
    self_report: SelfReportPolicy = field(default_factory=SelfReportPolicy)
    forge: ForgePolicy = field(default_factory=ForgePolicy)
    delivery: DeliveryPolicy = field(default_factory=DeliveryPolicy)
    supervisor: SupervisorPolicy = field(default_factory=SupervisorPolicy)

    def validate(self) -> None:
        if self.manager.provider not in PROVIDERS:
            raise ConfigError(f"manager.provider must be one of {PROVIDERS}")
        if self.worker.provider not in PROVIDERS:
            raise ConfigError(f"worker.provider must be one of {PROVIDERS}")
        if self.manager.reasoning not in REASONING_LEVELS:
            raise ConfigError(f"manager.reasoning must be one of {REASONING_LEVELS}")
        if self.worker.max_reasoning not in REASONING_LEVELS:
            raise ConfigError(f"worker.max_reasoning must be one of {REASONING_LEVELS}")
        if self.worker.default_model is None:
            self.worker.default_model = self.worker.max_model
        if self.worker.default_reasoning is None:
            self.worker.default_reasoning = self.worker.max_reasoning
        if self.worker.default_reasoning not in REASONING_LEVELS:
            raise ConfigError(f"worker.default_reasoning must be one of {REASONING_LEVELS}")
        if self.cost_posture not in COST_POSTURES:
            raise ConfigError(f"cost_posture must be one of {COST_POSTURES}")
        for name in ("model",):
            if not getattr(self.manager, name):
                raise ConfigError(f"manager.{name} must be set")
        if not self.worker.max_model:
            raise ConfigError("worker.max_model must be set")
        if not self.worker.default_model:
            raise ConfigError("worker.default_model must be set")
        if (
            isinstance(self.worker.max_concurrent, bool)
            or not isinstance(self.worker.max_concurrent, int)
            or self.worker.max_concurrent < 1
        ):
            raise ConfigError("worker.max_concurrent must be a positive integer")
        if (
            isinstance(self.worker.reconcile_slots, bool)
            or not isinstance(self.worker.reconcile_slots, int)
            or self.worker.reconcile_slots < 1
        ):
            raise ConfigError("worker.reconcile_slots must be a positive integer")
        if (
            isinstance(self.delivery.merge_after_hours, bool)
            or not isinstance(self.delivery.merge_after_hours, int)
            or self.delivery.merge_after_hours < 1
        ):
            raise ConfigError("delivery.merge_after_hours must be a positive integer")
        # Import locally to keep config serialization independent while using the
        # router's one authoritative model/reasoning comparison.
        from papaya_agent_runtime.router import CeilingError, WorkerProfile, enforce_ceiling

        try:
            enforce_ceiling(
                self,
                WorkerProfile(
                    self.worker.provider,
                    self.worker.default_model,
                    self.worker.default_reasoning,
                ),
            )
        except CeilingError as exc:
            raise ConfigError(f"worker default exceeds its ceiling: {exc}") from exc
        for name in (
            "completed_runs",
            "max_days",
            "cooldown_days",
            "minimum_runs",
            "failure_trigger_count",
            "max_actions",
        ):
            value = getattr(self.assessments, name)
            if value < 1:
                raise ConfigError(f"assessments.{name} must be at least 1")
        if self.assessments.minimum_runs > self.assessments.completed_runs:
            raise ConfigError("assessments.minimum_runs cannot exceed completed_runs")
        if self.assessments.max_actions > 3:
            raise ConfigError("assessments.max_actions cannot exceed 3")
        if self.health.quiet_minutes < 1:
            raise ConfigError("health.quiet_minutes must be at least 1")
        if self.health.plan_minutes < 1:
            raise ConfigError("health.plan_minutes must be at least 1")
        if self.health.checkin_after < 1:
            raise ConfigError("health.checkin_after must be at least 1")
        if self.health.push_by_minutes < 1:
            raise ConfigError("health.push_by_minutes must be at least 1")
        if self.health.rounds_interval < 0:
            raise ConfigError("health.rounds_interval must be zero or greater")
        if self.health.liveness_minutes < 1:
            raise ConfigError("health.liveness_minutes must be at least 1")
        if self.health.max_stale_stacks < 0:
            raise ConfigError("health.max_stale_stacks must be zero or greater")
        for name in ("input_ceiling_per_task", "input_ceiling_per_review"):
            value = getattr(self.usage, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ConfigError(f"usage.{name} must be a positive integer")
        for name in ("extra_tools", "dropped_tools", "locked", "allowed_tools"):
            value = getattr(self.claude, name)
            if value is None and name == "allowed_tools":
                continue
            if not isinstance(value, list):
                raise ConfigError(f"claude.{name} must be a list of strings")
            for entry in value:
                if not isinstance(entry, str) or not entry.strip():
                    raise ConfigError(
                        f"claude.{name} holds a bad entry {entry!r}; each one is a Claude "
                        "Code tool pattern such as 'Read' or 'Bash(uv:*)'"
                        if name != "locked"
                        else f"claude.locked holds a bad entry {entry!r}; name a key"
                    )
        max_per_day = self.self_report.max_per_day
        if isinstance(max_per_day, bool) or not isinstance(max_per_day, int) or max_per_day < 0:
            raise ConfigError("self_report.max_per_day must be zero or a positive integer")
        if not isinstance(self.self_report.enabled, bool):
            raise ConfigError("self_report.enabled must be true or false")
        if not isinstance(self.self_report.repo, str):
            raise ConfigError("self_report.repo must be a string such as 'owner/name'")
        stop_timeout = self.supervisor.stop_timeout
        if isinstance(stop_timeout, bool) or not isinstance(stop_timeout, int) or stop_timeout < 1:
            raise ConfigError("supervisor.stop_timeout must be a positive number of seconds")

    def to_dict(self) -> dict:
        """Every setting, resolved — what the runtime runs with, not what is stored."""
        claude = asdict(self.claude)
        if claude["allowed_tools"] is None:
            del claude["allowed_tools"]
        return {
            "manager": asdict(self.manager),
            "worker": asdict(self.worker),
            "cost_posture": self.cost_posture,
            "authority": asdict(self.authority),
            "tools": asdict(self.tools),
            "assessments": asdict(self.assessments),
            "health": asdict(self.health),
            "usage": asdict(self.usage),
            "claude": claude,
            "self_report": asdict(self.self_report),
            "forge": asdict(self.forge),
            "delivery": asdict(self.delivery),
            "supervisor": asdict(self.supervisor),
        }


def _from_dict(data: dict) -> MMConfig:
    manager = dict(data.get("manager", {}))
    worker = dict(data.get("worker", {}))
    # A role with no model gets its provider's default model, so setup never has to
    # write one down for the code's choice to apply.
    manager.setdefault("provider", ManagerProfile.provider)
    manager.setdefault("model", DEFAULT_MODELS.get(manager["provider"], ManagerProfile.model))
    # No worker provider means "whoever drives"; the runtime does not mix harnesses
    # unless a person said so. An existing file that names one explicitly is a
    # choice and is loaded verbatim.
    worker.setdefault("provider", manager["provider"])
    worker.setdefault("max_model", DEFAULT_MODELS.get(worker["provider"], WorkerCeiling.max_model))
    # A pre-defaults config inherits its former ceiling explicitly. That keeps old
    # custom-model configs loadable and deterministic without guessing a rank.
    worker.setdefault("default_model", worker.get("max_model", WorkerCeiling.max_model))
    worker.setdefault("default_reasoning", worker.get("max_reasoning", WorkerCeiling.max_reasoning))
    cfg = MMConfig(
        manager=ManagerProfile(**manager),
        worker=WorkerCeiling(**worker),
        cost_posture=data.get("cost_posture", "lean"),
        authority=Authority(**data.get("authority", {})),
        tools=ToolsPolicy(**data.get("tools", {})),
        assessments=AssessmentPolicy(**data.get("assessments", {})),
        health=HealthPolicy(**data.get("health", {})),
        usage=UsagePolicy(**data.get("usage", {})),
        claude=ClaudeProfile(**data.get("claude", {})),
        self_report=SelfReportPolicy(**data.get("self_report", {})),
        forge=ForgePolicy(**data.get("forge", {})),
        delivery=DeliveryPolicy(**data.get("delivery", {})),
        supervisor=SupervisorPolicy(**data.get("supervisor", {})),
    )
    return cfg


def load_config(path: Path | None = None) -> MMConfig:
    """Read the config, migrating an older file in place first.

    A file from before :data:`CONFIG_VERSION` 2 is rewritten once: its verbatim
    Claude tool list becomes deltas against the code's profile and every value
    equal to the code's default is removed, so the next release's defaults apply
    without anyone running anything. Each change is recorded as a ``config_change``
    event. An unreadable or invalid file is never rewritten.
    """
    p = path or config_path()
    if not p.exists():
        raise ConfigError(
            f"no config at {p}; run `ppy setup` to create the manager profile and worker ceiling"
        )
    with open(p, "rb") as fh:
        data = tomllib.load(fh)
    migrated, changes = migrate(data)
    cfg = _from_dict(migrated)
    cfg.validate()
    if changes:
        try:
            save_config(cfg, p)
        except OSError:  # a read-only home still loads; it migrates on the next writable load
            return cfg
        from papaya_agent_runtime import config_changes

        for change in changes:
            config_changes.record(**change)
    return cfg


def migrate(data: dict) -> tuple[dict, list[dict]]:
    """An older file's contents in today's shape, and what changed on the way.

    Pure: nothing is written. Each change is the keyword arguments of
    :func:`papaya_agent_runtime.config_changes.record`.
    """
    data = {k: (dict(v) if isinstance(v, dict) else v) for k, v in data.items()}
    version = data.pop("config_version", 1)
    changes: list[dict] = []
    claude = data.get("claude")
    if isinstance(claude, dict) and "allowed_tools" in claude:
        locked = claude.get("locked") or []
        if "allowed_tools" not in locked and isinstance(claude["allowed_tools"], list):
            change = _migrate_allowed_tools(claude)
            if change is not None:
                changes.append(change)
    if isinstance(version, int) and version >= CONFIG_VERSION:
        return data, changes
    try:
        before = _from_dict(data)
        before.validate()
    except (ConfigError, TypeError):
        return data, changes  # load reports it; an invalid file is never rewritten
    stored = _stored_dict(before)
    removed = sorted(set(_leaves(data)) - set(_leaves(stored)) - {"config_version"})
    changes.append(
        {
            "key": "config_version",
            "before": version,
            "after": CONFIG_VERSION,
            "why": (
                "stopped storing the built-in defaults, so a new release's defaults apply; "
                f"removed {', '.join(removed) if removed else 'nothing'}"
            ),
            "evidence": {"removed": removed},
        }
    )
    return stored, changes


def _migrate_allowed_tools(claude: dict) -> dict | None:
    """Turn a stored verbatim tool list into deltas against the code's profile."""
    stored_raw = [str(t) for t in claude.pop("allowed_tools")]
    stored = list(dict.fromkeys(normalise_tool(t) for t in stored_raw if t.strip()))
    current = list(CLAUDE_PROFILE)
    candidates = [*HISTORICAL_CLAUDE_PROFILES, ("current profile", tuple(current))]
    # The profile it differs least from; on a tie, the newer one.
    label, matched = min(
        reversed(candidates),
        key=lambda item: len(set(item[1]) ^ set(stored)),
    )
    extra = [t for t in stored if t not in matched and t not in current]
    dropped = [t for t in matched if t not in stored and t in current]
    claude["extra_tools"] = list(dict.fromkeys([*claude.get("extra_tools", []), *extra]))
    claude["dropped_tools"] = list(dict.fromkeys([*claude.get("dropped_tools", []), *dropped]))
    if not claude["extra_tools"]:
        del claude["extra_tools"]
    if not claude["dropped_tools"]:
        del claude["dropped_tools"]
    if not extra and not dropped:
        why = f"claude.allowed_tools was a copy of the {label}; removed, the code's profile applies"
    else:
        why = (
            f"claude.allowed_tools was the {label} with changes; kept as extra: "
            f"{', '.join(extra) or 'nothing'}; kept as dropped: {', '.join(dropped) or 'nothing'}"
        )
    return {
        "key": "claude.allowed_tools",
        "before": stored_raw,
        "after": {"extra_tools": extra, "dropped_tools": dropped},
        "why": why,
        "evidence": {"matched_profile": label},
    }


def _leaves(data: dict, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            out.update(_leaves(value, f"{prefix}{key}."))
        else:
            out[f"{prefix}{key}"] = value
    return out


def _resolved(data: dict) -> dict | None:
    try:
        cfg = _from_dict(data)
        cfg.validate()
    except (ConfigError, TypeError):
        return None
    return cfg.to_dict()


def _stored_dict(cfg: MMConfig) -> dict:
    """What belongs in the file: only the values the code would not arrive at itself.

    A key is kept exactly when removing it would change the loaded config — which
    covers the plain schema defaults and the resolved ones alike (a worker provider
    equal to the manager's, a model equal to its provider's default, a
    ``default_model`` equal to the ceiling).
    """
    target = cfg.to_dict()
    data = {k: (dict(v) if isinstance(v, dict) else v) for k, v in target.items()}
    for section, values in list(data.items()):
        if isinstance(values, dict):
            for key in list(values):
                trial = {k: (dict(v) if isinstance(v, dict) else v) for k, v in data.items()}
                del trial[section][key]
                if _resolved(trial) == target:
                    del data[section][key]
            if not data[section]:
                del data[section]
        else:
            trial = dict(data)
            del trial[section]
            if _resolved(trial) == target:
                del data[section]
    return {"config_version": CONFIG_VERSION, **data}


def effective_claude_tools(cfg: MMConfig) -> list[str]:
    """The code's profile, plus what a person or the runtime added, minus what was dropped."""
    if cfg.claude.allowed_tools is not None:  # a locked pre-delta list, used verbatim
        return list(cfg.claude.allowed_tools)
    dropped = set(cfg.claude.dropped_tools)
    tools = [t for t in CLAUDE_PROFILE if t not in dropped]
    tools += [t for t in cfg.claude.extra_tools if t not in tools and t not in dropped]
    return tools


def claude_tool_provenance(cfg: MMConfig) -> list[tuple[str, str]]:
    """Each tool pattern and where it comes from: ``profile``, ``extra`` or ``dropped``.

    A locked pre-delta list marks its entries ``stored``.
    """
    if cfg.claude.allowed_tools is not None:
        return [(t, "stored") for t in cfg.claude.allowed_tools]
    dropped = set(cfg.claude.dropped_tools)
    out = [(t, "dropped" if t in dropped else "profile") for t in CLAUDE_PROFILE]
    seen = {t for t, _ in out}
    for tool in cfg.claude.extra_tools:
        if tool not in seen:
            out.append((tool, "dropped" if tool in dropped else "extra"))
            seen.add(tool)
    return out


def is_locked(cfg: MMConfig, key: str) -> bool:
    """Has a person locked ``section.key`` against the runtime changing it?"""
    section, _, name = key.partition(".")
    locked = getattr(getattr(cfg, section, None), "locked", None) or []
    return name in locked


def default_worker_provider() -> str:
    """The provider a dispatch uses when the caller named none.

    It is the configured worker ceiling's provider — the same ceiling ``--model``
    and ``--reasoning`` are clamped to. It is deliberately never ``fake``: the
    dispatch ``--provider`` flag used to default to ``fake``, so a manager who
    omitted it on 2026-09-04 got a stub-writing worker that pushed a branch to a
    real GitHub remote within seconds (issue #49).

    With no readable config there is no ceiling to read, so this falls back to the
    schema default rather than guessing ``fake``; the dispatch then fails plainly
    at the ceiling check with "run `ppy setup` first". That schema default is the
    manager's own provider, so even the fallback never silently mixes harnesses.
    """
    try:
        return load_config().worker.provider
    except ConfigError:
        return WorkerCeiling().provider


def save_config(cfg: MMConfig, path: Path | None = None) -> Path:
    cfg.validate()
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_dumps_toml(_stored_dict(cfg)), encoding="utf-8")
    return p


def _toml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_value(value: object) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_toml_scalar(v) for v in value) + "]"
    return _toml_scalar(value)


def _dumps_toml(data: dict) -> str:
    """Serialize the known config shape: top-level scalars then [tables]."""
    lines: list[str] = []
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    for key, value in scalars.items():
        lines.append(f"{key} = {_toml_value(value)}")
    for table, values in tables.items():
        lines.append("")
        lines.append(f"[{table}]")
        for key, value in values.items():
            lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"
