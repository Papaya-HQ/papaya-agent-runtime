"""Worker routing and hard ceiling enforcement.

The runtime — not the manager prompt — enforces the configured worker ceiling.
A dispatch that names a model or reasoning keeps it; one that names neither is
routed by its brief (``routing.route``): a contract-heavy brief — an actual
database migration, new routes or endpoints, a state machine, or more than five
numbered In scope items — takes the ceiling's model and reasoning, anything else
the configured default. Every profile is checked here afterwards, so nothing is
ever routed above the ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass

from papaya_agent_runtime.config import MMConfig

# Ordered cheapest -> most capable. Unknown models compare as "unranked".
# The Codex names are a configured safety ordering, cheapest/least capable to
# most capable. It does not assert exact dollar costs. Unknown names remain
# unranked and are eligible only when they exactly match the configured ceiling.
MODEL_LADDERS: dict[str, list[str]] = {
    "claude": ["haiku", "sonnet", "opus"],
    "codex": ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-astra"],
}
REASONING_LADDER = ["low", "medium", "high", "xhigh"]


class CeilingError(Exception):
    """Raised when a requested worker profile exceeds the configured ceiling."""


@dataclass
class WorkerProfile:
    provider: str
    model: str
    reasoning: str


def _reasoning_rank(level: str) -> int:
    try:
        return REASONING_LADDER.index(level)
    except ValueError as exc:
        raise CeilingError(f"unknown reasoning level {level!r}") from exc


def _model_rank(provider: str, model: str) -> int | None:
    ladder = MODEL_LADDERS.get(provider, [])
    return ladder.index(model) if model in ladder else None


def enforce_ceiling(config: MMConfig, profile: WorkerProfile) -> None:
    """Raise CeilingError if ``profile`` exceeds the configured worker ceiling."""
    ceiling = config.worker
    if profile.provider != ceiling.provider:
        raise CeilingError(
            f"provider {profile.provider!r} does not match worker ceiling provider "
            f"{ceiling.provider!r}"
        )
    if _reasoning_rank(profile.reasoning) > _reasoning_rank(ceiling.max_reasoning):
        raise CeilingError(
            f"reasoning {profile.reasoning!r} exceeds ceiling {ceiling.max_reasoning!r}"
        )
    req = _model_rank(profile.provider, profile.model)
    cap = _model_rank(ceiling.provider, ceiling.max_model)
    if req is not None and cap is not None:
        if req > cap:
            raise CeilingError(f"model {profile.model!r} exceeds ceiling {ceiling.max_model!r}")
    elif profile.model != ceiling.max_model:
        # Either side unranked: only the ceiling model itself is eligible.
        raise CeilingError(
            f"model {profile.model!r} is not comparable within the {profile.provider} "
            f"ladder and is not the ceiling model {ceiling.max_model!r}"
        )


def choose_worker(config: MMConfig, *, complexity: str = "normal") -> WorkerProfile:
    """Return the explicit default worker profile and prove it is within policy.

    ``complexity`` remains accepted for callers compiled against the earlier API,
    but model selection is no longer implicit: task-specific lower-cost profiles
    are explicit dispatch choices.
    """
    _ = complexity
    profile = WorkerProfile(
        provider=config.worker.provider,
        model=config.worker.default_model or config.worker.max_model,
        reasoning=config.worker.default_reasoning or config.worker.max_reasoning,
    )
    enforce_ceiling(config, profile)  # defensive: selection must never exceed ceiling
    return profile
