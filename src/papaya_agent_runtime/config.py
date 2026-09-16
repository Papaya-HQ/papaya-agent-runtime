"""Configuration model for Papaya Agent Runtime (``.ppy/config.toml``).

Holds the manager profile, the hard worker ceiling, cost posture, and standing
repository authority. The runtime enforces these; prompts never do. Validation
is fail-closed: an unusable config raises rather than silently degrading.

The control plane keeps zero runtime package dependencies, so TOML is read with
the stdlib ``tomllib`` and written with a small purpose-built serializer for the
known, flat-ish schema.
"""

from __future__ import annotations

import os
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


def default_claude_allowed_tools() -> list[str]:
    """The documented Claude worker tool profile.

    Claude Code only gives a worker a shell when it is launched with
    ``--allowedTools``. That list used to come *only* from
    ``PPY_CLAUDE_ALLOWED_TOOLS`` in the supervisor's own environment and was
    persisted nowhere, so every ``ppy supervisor serve`` had to export it first and
    a dispatch after a restart silently produced a worker with no shell
    (2026-09-02).

    This is the profile the Claude Code permission classifier accepted on
    2026-09-02, proved end to end by smoke task 77: progress reporting, uv, a
    commit, and a push. A broader list including ``rm``, ``gh`` and ``export`` was
    classifier-blocked, so it is deliberately absent — and workers never open PRs
    or hold forge credentials anyway.

    The JavaScript toolchain, the file verbs and the read-only text tools arrived
    on 2026-09-16, when every worker dispatched into a JavaScript repository
    reported ``node --test`` denied and one could not ``cp`` its evidence into
    place: the list had been copied from a Python-only manager. Containment is the
    write boundary, not this verb list — a worker that may ``cp`` inside its
    worktree is still refused outside it.
    """
    from papaya_agent_runtime.manager.launch import repo_root

    return [
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
        f"Bash({os.path.join(repo_root(), 'bin', 'ppy')}:*)",
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
    ]


@dataclass
class ClaudeProfile:
    """Settings that shape a Claude worker's session."""

    allowed_tools: list[str] = field(default_factory=default_claude_allowed_tools)


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
    # Seconds between `ppy serve`'s rounds, when neither the flag nor the env says.
    rounds_interval: int = 300


@dataclass
class UsagePolicy:
    """Advisory input-token ceilings; these report, they never stop work."""

    input_ceiling_per_task: int = 12_000_000
    input_ceiling_per_review: int = 3_000_000


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
        if self.health.rounds_interval < 0:
            raise ConfigError("health.rounds_interval must be zero or greater")
        if self.health.max_stale_stacks < 0:
            raise ConfigError("health.max_stale_stacks must be zero or greater")
        for name in ("input_ceiling_per_task", "input_ceiling_per_review"):
            value = getattr(self.usage, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ConfigError(f"usage.{name} must be a positive integer")
        if not isinstance(self.claude.allowed_tools, list):
            raise ConfigError("claude.allowed_tools must be a list of tool patterns")
        for pattern in self.claude.allowed_tools:
            if not isinstance(pattern, str) or not pattern.strip():
                raise ConfigError(
                    f"claude.allowed_tools holds a bad entry {pattern!r}; each one is a Claude "
                    "Code tool pattern such as 'Read' or 'Bash(uv:*)'"
                )

    def to_dict(self) -> dict:
        return {
            "manager": asdict(self.manager),
            "worker": asdict(self.worker),
            "cost_posture": self.cost_posture,
            "authority": asdict(self.authority),
            "tools": asdict(self.tools),
            "assessments": asdict(self.assessments),
            "health": asdict(self.health),
            "usage": asdict(self.usage),
            "claude": asdict(self.claude),
        }


def _from_dict(data: dict) -> MMConfig:
    manager = dict(data.get("manager", {}))
    worker = dict(data.get("worker", {}))
    # No worker provider means "whoever drives"; the runtime does not mix harnesses
    # unless a person said so. An existing file that names one explicitly is a
    # choice and is loaded verbatim.
    worker.setdefault("provider", manager.get("provider", ManagerProfile.provider))
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
    )
    return cfg


def load_config(path: Path | None = None) -> MMConfig:
    p = path or config_path()
    if not p.exists():
        raise ConfigError(
            f"no config at {p}; run `ppy setup` to create the manager profile and worker ceiling"
        )
    with open(p, "rb") as fh:
        data = tomllib.load(fh)
    cfg = _from_dict(data)
    cfg.validate()
    return cfg


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
    p.write_text(_dumps_toml(cfg.to_dict()), encoding="utf-8")
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
