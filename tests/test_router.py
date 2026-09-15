"""Hermetic tests for worker routing and ceiling enforcement."""

from __future__ import annotations

import pytest

from papaya_agent_runtime.config import ManagerProfile, MMConfig, WorkerCeiling
from papaya_agent_runtime.router import (
    CeilingError,
    WorkerProfile,
    choose_worker,
    enforce_ceiling,
)


def _config(
    max_model="sonnet",
    max_reasoning="medium",
    default_model="haiku",
    default_reasoning="low",
):
    return MMConfig(
        manager=ManagerProfile("claude", "opus", "high"),
        worker=WorkerCeiling("claude", max_model, max_reasoning, default_model, default_reasoning),
    )


def test_choose_worker_picks_smallest() -> None:
    profile = choose_worker(_config())
    assert profile.model == "haiku"
    assert profile.reasoning == "low"


def test_enforce_rejects_model_over_ceiling() -> None:
    with pytest.raises(CeilingError):
        enforce_ceiling(_config("sonnet"), WorkerProfile("claude", "opus", "low"))


def test_enforce_rejects_reasoning_over_ceiling() -> None:
    with pytest.raises(CeilingError):
        enforce_ceiling(_config(max_reasoning="medium"), WorkerProfile("claude", "haiku", "high"))


def test_enforce_allows_at_ceiling() -> None:
    enforce_ceiling(_config("sonnet", "medium"), WorkerProfile("claude", "sonnet", "medium"))


def test_unknown_model_rejected_unless_ceiling() -> None:
    with pytest.raises(CeilingError):
        enforce_ceiling(_config("sonnet"), WorkerProfile("claude", "mystery", "low"))


def _codex_config(
    max_model="gpt-5-codex",
    max_reasoning="medium",
    default_model=None,
    default_reasoning=None,
):
    return MMConfig(
        manager=ManagerProfile("claude", "opus", "high"),
        worker=WorkerCeiling("codex", max_model, max_reasoning, default_model, default_reasoning),
    )


def test_codex_chooses_configured_default_not_a_cheaper_model_implicitly() -> None:
    profile = choose_worker(_codex_config("gpt-5.6-sol", "xhigh", "gpt-5.6-sol", "high"))
    assert profile.model == "gpt-5.6-sol"
    assert profile.reasoning == "high"
    assert choose_worker(_codex_config("gpt-5.6-sol"), complexity="hard").model == "gpt-5.6-sol"


def test_codex_rejects_model_other_than_ceiling() -> None:
    config = _codex_config("gpt-5.6-sol")
    enforce_ceiling(config, WorkerProfile("codex", "gpt-5.6-luna", "low"))
    enforce_ceiling(config, WorkerProfile("codex", "gpt-5.6-terra", "medium"))
    enforce_ceiling(_codex_config("gpt-5.6-sol"), WorkerProfile("codex", "gpt-5.6-sol", "medium"))
    with pytest.raises(CeilingError):
        enforce_ceiling(config, WorkerProfile("codex", "gpt-6-astra", "low"))
    with pytest.raises(CeilingError):
        enforce_ceiling(config, WorkerProfile("codex", "unknown-codex", "low"))


def test_different_real_provider_never_bypasses_ceiling() -> None:
    with pytest.raises(CeilingError, match="does not match"):
        enforce_ceiling(_codex_config("gpt-5.6-sol"), WorkerProfile("claude", "haiku", "low"))


def test_unranked_claude_ceiling_only_allows_itself() -> None:
    # A ranked request against an unranked ceiling must not slip through.
    with pytest.raises(CeilingError):
        enforce_ceiling(_config("custom-model"), WorkerProfile("claude", "haiku", "low"))
    assert choose_worker(_config("custom-model", default_model="custom-model")).model == (
        "custom-model"
    )
