"""Hermetic tests for the tiny schema validator and the project schemas."""

from __future__ import annotations

import pytest

from papaya_agent_runtime.schemas import (
    SchemaError,
    validate_task,
    validate_worker_result,
)


def test_valid_task_packet() -> None:
    validate_task({"title": "do it", "repo": "widgets", "instructions": "x", "provider": "claude"})


def test_task_missing_required() -> None:
    with pytest.raises(SchemaError):
        validate_task({"title": "do it", "repo": "widgets"})


def test_task_bad_provider_enum() -> None:
    with pytest.raises(SchemaError):
        validate_task({"title": "t", "repo": "r", "instructions": "x", "provider": "gemini"})


def test_task_additional_property_rejected() -> None:
    with pytest.raises(SchemaError):
        validate_task(
            {"title": "t", "repo": "r", "instructions": "x", "provider": "fake", "surprise": 1}
        )


def test_worker_result_valid() -> None:
    validate_worker_result(
        {
            "status": "completed",
            "summary": "ok",
            "usage": {"provider": "codex", "input_tokens": 1, "output_tokens": 2},
        }
    )


def test_worker_result_bad_status() -> None:
    with pytest.raises(SchemaError):
        validate_worker_result({"status": "kinda-done"})
