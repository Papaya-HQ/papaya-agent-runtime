"""The runtime edits its own configuration, and says so.

On 2026-09-16 a person had to type `ppy config claude --reset` because their
`config.toml` held a copy of the tool profile from the day the runtime set itself up.
PR 19 had widened the profile, the stored copy kept winning, and readiness could only
warn. Configuration is the runtime's to keep right, so a finding with a safe remedy is
*applied* — at `ppy serve` start and whenever a repository is ensured — rather than
described to somebody who then has to run a command.

Two remedies exist today:

- a registered repository's gate runs a program whose profile pattern was dropped
  (`claude_tools_lack_gate`): the pattern is restored;
- a worker was denied a command in the documented safe family
  (:mod:`papaya_agent_runtime.tool_learning`): its pattern is added.

Every change — these, a load-time migration, and a person's own `ppy config claude`
— is a `config_change` event carrying key, before, after, why and evidence;
`ppy config history` lists them. A person stops the runtime changing a key by locking
it (`claude.locked`); a remedy a lock refuses becomes a readiness warning naming the
lock, and nothing is changed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: One change to the configuration, by whoever made it.
CONFIG_CHANGE = "config_change"
#: Marks how far `ppy serve` has printed the history, so each change is said once.
ANNOUNCED = "config_changes_announced"


def record(
    *,
    key: str,
    before: Any,
    after: Any,
    why: str,
    evidence: dict | None = None,
) -> int | None:
    """Write one `config_change` event; never raises."""
    from papaya_agent_runtime.state import init_db, store

    payload = {"key": key, "before": before, "after": after, "why": why, "evidence": evidence or {}}
    try:
        return store.append_event(init_db(), kind=CONFIG_CHANGE, payload=payload)
    except Exception as exc:  # noqa: BLE001 - recording must never undo the change itself
        log.warning("could not record config change to %s: %s", key, exc)
        return None


def history(*, after_id: int = 0, limit: int | None = None) -> list[dict]:
    """Every recorded change, oldest first: ``id``, ``at`` and the payload."""
    from papaya_agent_runtime.state import init_db

    sql = "SELECT id, payload, created_at FROM events WHERE kind = ? AND id > ? ORDER BY id"
    rows = init_db().execute(sql, (CONFIG_CHANGE, after_id)).fetchall()
    entries = [
        {"id": int(row["id"]), "at": row["created_at"], **json.loads(row["payload"])}
        for row in rows
    ]
    return entries[-limit:] if limit else entries


def line(entry: dict) -> str:
    """One change, in one line a person can read."""
    return f"config: {entry['key']} — {entry['why']}"


def unannounced() -> list[dict]:
    """Changes `ppy serve` has not printed yet."""
    from papaya_agent_runtime.state import init_db

    row = (
        init_db()
        .execute("SELECT payload FROM events WHERE kind = ? ORDER BY id DESC LIMIT 1", (ANNOUNCED,))
        .fetchone()
    )
    after = int(json.loads(row["payload"]).get("last_id", 0)) if row else 0
    return history(after_id=after)


def mark_announced(entries: list[dict]) -> None:
    if not entries:
        return
    from papaya_agent_runtime.state import init_db, store

    store.append_event(init_db(), kind=ANNOUNCED, payload={"last_id": entries[-1]["id"]})


# ── remedies ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Remedy:
    """One change the runtime would make to the Claude tool deltas."""

    pattern: str
    #: ``undrop`` takes a profile pattern out of ``dropped_tools``; ``add`` puts a
    #: pattern into ``extra_tools``.
    action: str
    why: str
    evidence: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return "claude.dropped_tools" if self.action == "undrop" else "claude.extra_tools"

    def lock(self, cfg) -> str | None:
        """The locked key that refuses this remedy, if one does."""
        from papaya_agent_runtime.config import is_locked

        if cfg.claude.allowed_tools is not None:
            return "claude.allowed_tools"
        return self.key if is_locked(cfg, self.key) else None


def planned(cfg) -> list[Remedy]:
    """What the runtime would change right now, from local state only."""
    from papaya_agent_runtime import readiness, tool_learning
    from papaya_agent_runtime.config import CLAUDE_PROFILE, effective_claude_tools

    effective = set(effective_claude_tools(cfg))
    remedies: dict[str, Remedy] = {}

    for pattern, repos in sorted(readiness.gate_tool_needs().items()):
        if pattern in CLAUDE_PROFILE and pattern not in effective:
            remedies[pattern] = Remedy(
                pattern=pattern,
                action="undrop",
                why=f"the gate of {', '.join(repos)} runs it and it was dropped from the profile",
                evidence={"repositories": repos},
            )
    for denial in tool_learning.learnable():
        pattern = denial["pattern"]
        if pattern in effective or pattern in remedies:
            continue
        in_profile = pattern in CLAUDE_PROFILE
        remedies[pattern] = Remedy(
            pattern=pattern,
            action="undrop" if in_profile else "add",
            why=f"a worker was denied `{denial['command']}`, which is in the safe family",
            evidence={
                "task_id": denial.get("task_id"),
                "event_id": denial.get("event_id"),
                "command": denial["command"],
            },
        )
    return list(remedies.values())


def blocked(cfg) -> list[tuple[Remedy, str]]:
    """Remedies a lock refuses, each with the lock that refuses it."""
    return [(r, lock) for r in planned(cfg) if (lock := r.lock(cfg)) is not None]


def apply(*, context: str) -> list[dict]:
    """Make every unlocked remedy, record each, and return what changed. Never raises."""
    from papaya_agent_runtime.config import ConfigError, load_config, save_config
    from papaya_agent_runtime.paths import config_path

    if not config_path().exists():
        return []
    try:
        cfg = load_config()
        todo = [r for r in planned(cfg) if r.lock(cfg) is None]
        if not todo:
            return []
        changes = []
        for remedy in todo:
            before = list(getattr(cfg.claude, remedy.key.split(".")[1]))
            if remedy.action == "undrop":
                cfg.claude.dropped_tools = [t for t in before if t != remedy.pattern]
                after = cfg.claude.dropped_tools
            else:
                cfg.claude.extra_tools = [*before, remedy.pattern]
                after = cfg.claude.extra_tools
            changes.append(
                {
                    "key": remedy.key,
                    "before": before,
                    "after": list(after),
                    "why": f"{'restored' if remedy.action == 'undrop' else 'added'} "
                    f"{remedy.pattern}: {remedy.why} ({context})",
                    "evidence": remedy.evidence,
                }
            )
        save_config(cfg)
    except (ConfigError, OSError) as exc:
        log.warning("could not apply config remedies (%s): %s", context, exc)
        return []
    except Exception as exc:  # noqa: BLE001 - keeping config right must never stop the runtime
        log.warning("config remedies failed (%s): %s", context, exc)
        return []
    recorded = []
    for change in changes:
        event_id = record(**change)
        recorded.append({"id": event_id, **change})
    return recorded


__all__ = [
    "ANNOUNCED",
    "CONFIG_CHANGE",
    "Remedy",
    "apply",
    "blocked",
    "history",
    "line",
    "mark_announced",
    "planned",
    "record",
    "unannounced",
]
