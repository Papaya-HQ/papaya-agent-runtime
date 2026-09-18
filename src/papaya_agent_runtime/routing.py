"""Which worker tier a brief goes to when the dispatch names no model.

Dispatch used to give every unpinned task the configured default worker. In
Middle Manager that was wrong for briefs whose correctness depends on holding a
multi-section contract in view: contract-heavy briefs on the cheapest tier were
closed twice for under-delivery and stopped once mid-implementation, while the
one top-tier dispatch was approved first time (gizm0duck/middle-manager #78).
The runtime's unattended manager writes the same kind of brief (runtime #94,
item 6).

So a brief that adds a database migration, names new routes or endpoints,
specifies a state machine, or lists more than five numbered items under In
scope is routed to the top of the configured ladder — the ceiling's model and
reasoning — unless ``--model`` or ``--reasoning`` says otherwise. Anything else
keeps the configured default. The dispatch response, its ``dispatched`` event
and ``ppy dispatch`` all carry the rule that fired. Nothing here can exceed the
ceiling: the top tier *is* the ceiling, and ``router.enforce_ceiling`` still
checks every profile afterwards.

The migration signal is deliberately narrower than Middle Manager's bare word
"migration", which sent a brief to the top tier for mentioning "the migration
advisory". It needs an actual migration: a path under a ``migrations`` or
``alembic/versions`` directory, ``down_revision``, or an instruction to add or
write a (new) migration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from papaya_agent_runtime.config import MMConfig

#: More numbered In scope items than this, and the brief is a contract, not a chore.
NUMBERED_SCOPE_THRESHOLD = 5

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s*(.+?)\s*#*\s*$")
_NUMBERED = re.compile(r"^\s{0,3}\d+[.)]\s+\S")
_SCOPE_HEADING = re.compile(r"^(?:in[ -]scope|scope)\b")

_MIGRATION_PATH = re.compile(r"(?:^|[/\s`'\"(])(?:migrations|alembic/versions)/[\w.-]+")
_DOWN_REVISION = re.compile(r"\bdown_revision\b")
# "add a migration", "write an Alembic migration", "a new schema migration" —
# never the advisory, the collision check or the word on its own.
_MIGRATION_INSTRUCTION = re.compile(
    r"(?:\b(?:add|adds|adding|write|writes|writing|create|creates|creating)\s+an?\s+"
    r"|\bnew\s+)"
    r"(?:new\s+)?(?:[\w-]+\s+)?migrations?\b(?!\s+(?:advisor|collision|check))",
    re.IGNORECASE,
)
_NEW_ROUTES = re.compile(
    r"\b(?:new|add|adds|added|adding|create|creates|expose|exposes|introduce|introduces)\b"
    r"[^.\n]{0,60}?\b(?:routes?|endpoints?)\b",
    re.IGNORECASE,
)
_STATE_MACHINE = re.compile(
    r"\bstate[ -]machines?\b|\bstate[ -]transitions?\b|\btransition (?:table|truth table)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Route:
    model: str
    reasoning: str
    #: ``"explicit"`` (the caller chose), ``"top-tier"`` or ``"default"``.
    tier: str
    #: Why, in one clause the dispatch output prints.
    rule: str

    def as_dict(self) -> dict:
        return {
            "tier": self.tier,
            "model": self.model,
            "reasoning": self.reasoning,
            "rule": self.rule,
            "line": describe(self),
        }


def contract_signals(text: str) -> list[str]:
    """The reasons a brief counts as contract-heavy, in a stable order; empty when none."""
    reasons: list[str] = []
    if (
        _MIGRATION_PATH.search(text)
        or _DOWN_REVISION.search(text)
        or _MIGRATION_INSTRUCTION.search(text)
    ):
        reasons.append("adds a database migration")
    if _NEW_ROUTES.search(text):
        reasons.append("names new routes or endpoints")
    if _STATE_MACHINE.search(text):
        reasons.append("specifies a state machine")
    numbered = _numbered_scope_items(text)
    if numbered > NUMBERED_SCOPE_THRESHOLD:
        reasons.append(f"lists {numbered} numbered scope items")
    return reasons


def _numbered_scope_items(text: str) -> int:
    """Numbered list items under an In scope / Scope heading (Out of scope excluded)."""
    count = 0
    in_scope = False
    for raw in text.splitlines():
        heading = _HEADING.match(raw)
        if heading is not None:
            in_scope = bool(_SCOPE_HEADING.match(heading.group(2).strip().lower()))
            continue
        if in_scope and _NUMBERED.match(raw):
            count += 1
    return count


def route(
    config: MMConfig,
    *,
    provider: str,
    instructions: str,
    model: str | None,
    reasoning: str | None,
) -> Route:
    """The worker profile a dispatch runs with, and the rule that chose it.

    An explicit model or reasoning is kept as given (the ceiling check happens
    after, as before). Otherwise a contract-heavy brief for the ceiling's own
    provider takes the ceiling's model and reasoning; anything else takes the
    configured default.
    """
    ceiling = config.worker
    default_model = ceiling.default_model or ceiling.max_model
    default_reasoning = ceiling.default_reasoning or ceiling.max_reasoning
    if model or reasoning:
        return Route(
            model=model or default_model,
            reasoning=reasoning or default_reasoning,
            tier="explicit",
            rule="chosen by --model/--reasoning",
        )
    reasons = contract_signals(instructions or "") if provider == ceiling.provider else []
    if reasons:
        return Route(
            model=ceiling.max_model,
            reasoning=ceiling.max_reasoning,
            tier="top-tier",
            rule="contract-heavy brief: " + "; ".join(reasons),
        )
    return Route(
        model=default_model,
        reasoning=default_reasoning,
        tier="default",
        rule="no contract signals in the brief; the configured default tier",
    )


def describe(route: Route) -> str:
    return f"routing: {route.tier} ({route.model}/{route.reasoning}) — {route.rule}"
